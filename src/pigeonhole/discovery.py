from __future__ import annotations

import json
import asyncio
import re
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from pigeonhole.config import LLMConfig
from pigeonhole.contracts import (
    Action,
    Checkpoint,
    FailureCode,
    InputValue,
    LiteralValue,
    Recovery,
    Risk,
    StepDiagnostic,
    TargetBundle,
)
from pigeonhole.surface.base import SurfaceDriver


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    intent: str = ""
    action: str | None = None
    ref: str | None = None
    input_name: str | None = None
    literal: str | float | int | bool | None = None
    output: str | None = None
    checkpoint_text: str | None = None
    risk: Risk = Risk.SAFE
    reason: str | None = None


class RecordedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    intent: str
    action: Action
    target: TargetBundle | None = None
    checkpoint: Checkpoint | None = None
    recover: list[Recovery] = Field(default_factory=list)
    risk: Risk
    before_digest: str
    after_digest: str


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0.0"
    goal: str
    target: str
    model: str
    started_at: datetime
    completed_at: datetime
    steps: list[RecordedStep]
    output_names: list[str]
    success: bool
    stop_reason: str


class DiscoveryResult(BaseModel):
    status: str
    recording: Recording
    outputs: dict[str, Any]
    llm_calls: int


class DecisionModel(Protocol):
    name: str

    async def decide(self, prompt: str) -> Decision: ...


class OpenAICompatibleModel:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.name = config.model

    async def decide(self, prompt: str) -> Decision:
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "tools": DISCOVERY_TOOLS,
            "tool_choice": "required",
            "messages": [
                {"role": "system", "content": DISCOVERY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=90) as client:
            for attempt in range(4):
                response = await client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json=payload,
                )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt == 3:
                    break
                delay = float(2**attempt)
                if response.status_code == 429:
                    match = re.search(
                        r"retry in ([0-9.]+)s", response.text, flags=re.IGNORECASE
                    )
                    if match:
                        delay = max(delay, float(match.group(1)) + 1)
                await asyncio.sleep(delay)
            if response.is_error:
                raise RuntimeError(
                    f"model request failed ({response.status_code}): "
                    f"{response.text[:500]}"
                )
        message = response.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            raise RuntimeError("model did not return a tool call")
        call = calls[0]["function"]
        data = json.loads(call.get("arguments") or "{}")
        if not isinstance(data, dict):
            data = {}
        data["kind"] = call.get("name") or "stuck"
        return _decision_from_payload(data)


def _decision_from_payload(data: dict[str, Any]) -> Decision:
    payload = dict(data)
    if "checkpoint" in payload and "checkpoint_text" not in payload:
        checkpoint = payload.pop("checkpoint")
        payload["checkpoint_text"] = (
            checkpoint.get("expected") or checkpoint.get("text")
            if isinstance(checkpoint, dict)
            else checkpoint
        )
    if "output_name" in payload and "output" not in payload:
        payload["output"] = payload.pop("output_name")
    if "input" in payload and "input_name" not in payload:
        payload["input_name"] = payload.pop("input")
    allowed = set(Decision.model_fields)
    for key in list(payload):
        if key not in allowed:
            payload.pop(key)
    return Decision.model_validate(payload)


DISCOVERY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Act on one ephemeral observation ref. Never invent CSS or selectors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["click", "type", "select", "extract", "dismiss"],
                    },
                    "ref": {
                        "type": "string",
                        "description": "Ephemeral control ref from the observation, e.g. c3",
                    },
                    "input_name": {"type": ["string", "null"]},
                    "literal": {
                        "type": ["string", "number", "boolean", "null"]
                    },
                    "output": {"type": ["string", "null"]},
                    "checkpoint_text": {"type": ["string", "null"]},
                    "risk": {
                        "type": "string",
                        "enum": ["safe", "mutating", "irreversible"],
                    },
                    "reason": {"type": ["string", "null"]},
                },
                "required": ["intent", "action", "ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call only after every required output has been extracted.",
            "parameters": {
                "type": "object",
                "properties": {"intent": {"type": "string"}},
                "required": ["intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stuck",
            "description": "Call when the UI cannot be progressed safely.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


DISCOVERY_SYSTEM_PROMPT = """You operate an unfamiliar UI to accomplish one goal.
You receive a hybrid observation with ephemeral refs (c0, c1, …). Each ref includes
nearby text, frame, element type and geometry. Choose a ref from the observation.
Never invent CSS, selectors, or refs that are not listed.

Call exactly one tool:
- act: click, type, select, extract, or dismiss a listed ref
- done: only after every required output was extracted via act/extract
- stuck: the UI cannot be progressed safely

Rules:
- To type, set input_name to a declared input; never send the secret value.
- To extract, set output to a declared output name, never the visible value.
- has_value=true means a field is already filled; do not type it again.
- For a click that changes screens, set checkpoint_text to visible destination text.
- Treat the current observation as authoritative.
- If feedback says the UI did not change, pick a different control or call stuck.
"""


EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop_event(_: str, __: dict[str, Any]) -> None:
    return None


class DiscoveryLoop:
    def __init__(
        self,
        surface: SurfaceDriver,
        model: DecisionModel,
        policy: Any,
        *,
        max_steps: int = 20,
        event_sink: EventSink = _noop_event,
        confirmed_risks: set[Risk] | None = None,
        handoff: Any | None = None,
        evidence: Any | None = None,
        on_intervention: Callable[[Any], Awaitable[bool]] | None = None,
        capability_id: str = "discovery",
        redact_values: list[str] | None = None,
    ) -> None:
        self.surface = surface
        self.model = model
        self.policy = policy
        self.max_steps = max_steps
        self.event = event_sink
        self.confirmed_risks = confirmed_risks or set()
        self.handoff = handoff
        self.evidence = evidence
        self.on_intervention = on_intervention
        self.capability_id = capability_id
        self.redact_values = redact_values or []

    async def run(
        self,
        *,
        goal: str,
        target: str,
        inputs: dict[str, Any],
        input_names: list[str],
        required_outputs: list[str],
        hints: list[str] | None = None,
        preferred_frame: str | None = None,
        click_recoveries: list[Recovery] | None = None,
    ) -> DiscoveryResult:
        started = datetime.now(UTC)
        guidance: dict[str, Any] = {}
        if preferred_frame:
            guidance["preferred_frame"] = preferred_frame
        if hints:
            guidance["notes"] = list(hints)
        await self.surface.act("navigate", value=target)
        steps: list[RecordedStep] = []
        outputs: dict[str, Any] = {}
        llm_calls = 0
        stop_reason = "max_steps"
        success = False
        stagnant_actions = 0
        feedback = ""

        for index in range(self.max_steps):
            observation = await self.surface.observe()
            await self.event(
                "observed",
                {
                    "step": index + 1,
                    "url": observation.url,
                    "digest": observation.digest,
                    "visible_text": observation.visible_text,
                },
            )
            prompt = json.dumps(
                {
                    "goal": goal,
                    **({"target_guidance": guidance} if guidance else {}),
                    "declared_inputs": input_names,
                    "required_outputs": required_outputs,
                    "outputs_already_extracted": list(outputs),
                    "recent_actions": [
                        {
                            "action": step.action.type,
                            "intent": step.intent,
                            "checkpoint": (
                                step.checkpoint.expected if step.checkpoint else None
                            ),
                        }
                        for step in steps[-6:]
                    ],
                    "feedback": feedback,
                    "observation": observation.model_dump(mode="json"),
                }
            )
            decision = await self.model.decide(prompt)
            llm_calls += 1
            await self.event(
                "model_decided",
                {
                    "kind": decision.kind,
                    "intent": decision.intent,
                    "action": decision.action,
                    "ref": decision.ref,
                    "input_name": decision.input_name,
                    "output": decision.output,
                },
            )

            if decision.kind == "done":
                missing_now = set(required_outputs) - set(outputs)
                if missing_now:
                    feedback = (
                        "Do not declare done yet. Use action='extract' on the "
                        f"visible value ref and output one of {sorted(missing_now)}."
                    )
                    continue
                success = True
                stop_reason = "goal_met"
                break
            if decision.kind == "stuck":
                stop_reason = decision.reason or "agent_stuck"
                if await self._intervene(
                    goal=goal,
                    reason=stop_reason,
                    step_id=steps[-1].id if steps else None,
                ):
                    stagnant_actions = 0
                    feedback = (
                        "Operator returned the session. Continue from the current UI."
                    )
                    continue
                break
            if decision.kind != "act" or not decision.action:
                feedback = "Invalid decision: choose act, done, or stuck."
                continue
            if decision.action in {"type", "select"} and not (
                decision.input_name or decision.literal is not None
            ):
                feedback = (
                    f"{decision.action} requires input_name from declared_inputs "
                    "or a non-sensitive literal. Do not repeat the source-less action."
                )
                continue
            if decision.action in {"click", "type", "select", "extract", "dismiss"}:
                if not decision.ref:
                    feedback = f"{decision.action} requires an observation ref."
                    continue
                known_refs = {control.ref for control in observation.controls}
                if decision.ref not in known_refs:
                    if not known_refs:
                        feedback = (
                            "No interactive controls were observed yet. Wait for "
                            "the page to finish loading; do not invent refs."
                        )
                    else:
                        feedback = (
                            f"Unknown ref {decision.ref}. Choose a ref from the "
                            "current observation only."
                        )
                    continue
            if decision.action == "extract" and not decision.output:
                feedback = "extract requires one declared output name."
                continue
            if decision.action == "extract" and decision.output not in required_outputs:
                feedback = (
                    f"extract output must be one of {sorted(required_outputs)}, "
                    f"not {decision.output!r}. Use the declared name, never the "
                    "visible value."
                )
                continue
            if decision.action == "click" and not decision.checkpoint_text:
                feedback = (
                    "screen-changing click requires checkpoint_text describing "
                    "the visible destination state."
                )
                continue
            if decision.action == "click" and decision.ref and decision.checkpoint_text:
                clicked = next(
                    (
                        control
                        for control in observation.controls
                        if control.ref == decision.ref
                    ),
                    None,
                )
                labels = {
                    (clicked.visible_text or "").strip(),
                    (clicked.accessible_name or "").strip(),
                } if clicked else set()
                if decision.checkpoint_text.strip() in labels:
                    feedback = (
                        "checkpoint_text must describe the destination screen, "
                        "not the control you just clicked."
                    )
                    continue
            if (
                decision.risk != Risk.SAFE
                and decision.risk not in self.confirmed_risks
            ):
                decision = decision.model_copy(update={"risk": Risk.SAFE})

            policy_decision = self.policy.evaluate(
                url=observation.url,
                action=decision.action,
                risk=decision.risk,
                intent=decision.intent,
            )
            await self.event("policy_evaluated", policy_decision.model_dump(mode="json"))
            if (
                policy_decision.disposition == "confirm"
                and decision.risk in self.confirmed_risks
            ):
                await self.event(
                    "human_confirmed",
                    {"risk": decision.risk, "intent": decision.intent},
                )
            elif policy_decision.disposition != "allow":
                feedback = f"Policy {policy_decision.disposition}: {policy_decision.reason}"
                continue

            try:
                target_bundle = (
                    await self.surface.harvest(decision.ref)
                    if decision.ref is not None
                    else None
                )
            except Exception as exc:
                feedback = f"Could not harvest ref {decision.ref}: {exc}"
                await self.event("action_failed", {"error": feedback})
                continue
            value_source = None
            runtime_value = None
            if decision.input_name:
                if decision.input_name not in inputs:
                    feedback = f"Input {decision.input_name} was not supplied."
                    continue
                value_source = InputValue(name=decision.input_name)
                runtime_value = inputs[decision.input_name]
            elif decision.literal is not None:
                value_source = LiteralValue(value=decision.literal)
                runtime_value = decision.literal
            action = Action(
                type=decision.action,
                value=value_source,
                output=decision.output,
            )
            checkpoint = (
                Checkpoint(
                    id=f"cp{index + 1}",
                    kind="visible_text",
                    expected=decision.checkpoint_text,
                )
                if decision.checkpoint_text
                else None
            )
            try:
                extracted = await self.surface.act(
                    decision.action, decision.ref, runtime_value
                )
                if checkpoint is not None and not await self.surface.checkpoint(
                    checkpoint
                ):
                    feedback = (
                        "Destination checkpoint was not visible after the action. "
                        "Use checkpoint_text from the new screen, not the control "
                        "you clicked."
                    )
                    await self.event("action_failed", {"error": feedback})
                    continue
            except Exception as exc:
                feedback = f"Action failed: {type(exc).__name__}: {exc}"
                await self.event("action_failed", {"error": feedback})
                continue
            if decision.output:
                outputs[decision.output] = extracted

            after = await self.surface.observe()
            recoveries: list[Recovery] = []
            if decision.action == "click":
                recoveries = list(click_recoveries or [])
            steps.append(
                RecordedStep(
                    id=f"s{len(steps) + 1}",
                    intent=decision.intent,
                    action=action,
                    target=target_bundle,
                    checkpoint=checkpoint,
                    recover=recoveries,
                    risk=decision.risk,
                    before_digest=observation.digest,
                    after_digest=after.digest,
                )
            )
            await self.event(
                "acted",
                {
                    "step_id": steps[-1].id,
                    "action": decision.action,
                    "after_digest": after.digest,
                },
            )
            if (
                decision.action in {"click", "type", "select", "dismiss"}
                and after.digest == observation.digest
            ):
                stagnant_actions += 1
                feedback = (
                    "The UI state did not change. Do not repeat this action; "
                    "choose the next required control or report stuck."
                )
            else:
                stagnant_actions = 0
                feedback = ""
            if stagnant_actions >= 3:
                stop_reason = "dead_end"
                if await self._intervene(
                    goal=goal,
                    reason="UI state did not change after repeated actions",
                    step_id=steps[-1].id if steps else None,
                ):
                    stagnant_actions = 0
                    feedback = (
                        "Operator returned the session. Continue from the current UI."
                    )
                    continue
                break

        completed = datetime.now(UTC)
        recording = Recording(
            goal=goal,
            target=target,
            model=self.model.name,
            started_at=started,
            completed_at=completed,
            steps=steps,
            output_names=required_outputs,
            success=success,
            stop_reason=stop_reason,
        )
        return DiscoveryResult(
            status="success" if success else "stopped",
            recording=recording,
            outputs=outputs,
            llm_calls=llm_calls,
        )

    async def _intervene(
        self, *, goal: str, reason: str, step_id: str | None
    ) -> bool:
        diagnostic = StepDiagnostic(
            step_id=step_id,
            code=FailureCode.ACTION_FAILED,
            message=reason,
            expected=goal,
        )
        await self.event(
            "stuck",
            diagnostic.model_dump(mode="json", exclude_none=True),
        )
        if self.handoff is None or self.evidence is None:
            return False
        await self.surface.pause()
        payload = diagnostic.model_dump(mode="json", exclude_none=True)
        if hasattr(self.evidence, "redactor"):
            payload = self.evidence.redactor.data(payload)
        intervention = await self.handoff.raise_intervention(
            capability_id=self.capability_id,
            goal=goal,
            diagnostic=payload,
            evidence_directory=self.evidence.directory,
            redact_values=self.redact_values,
        )
        if self.on_intervention is None:
            return False
        return bool(await self.on_intervention(intervention))

