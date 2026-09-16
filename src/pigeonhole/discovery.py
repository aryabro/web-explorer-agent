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
    InputValue,
    LiteralValue,
    Recovery,
    Risk,
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
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": DISCOVERY_SYSTEM_PROMPT,
                },
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
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(content)
        for raw_key in list(data):
            kind = raw_key.lower()
            nested = data.get(raw_key)
            if kind in {"act", "done", "stuck"} and isinstance(nested, dict):
                data.pop(raw_key)
                data = {**nested, **data, "kind": kind}
                break
        if "checkpoint" in data and "checkpoint_text" not in data:
            checkpoint = data.pop("checkpoint")
            data["checkpoint_text"] = (
                checkpoint.get("expected") or checkpoint.get("text")
                if isinstance(checkpoint, dict)
                else checkpoint
            )
        if "output_name" in data and "output" not in data:
            data["output"] = data.pop("output_name")
        if "input" in data and "input_name" not in data:
            data["input_name"] = data.pop("input")
        # Models often smuggle result values onto done, e.g. {"kind":"done","holds_count":"2"}.
        # Keep only Decision fields so validation never crashes the run.
        allowed = set(Decision.model_fields)
        for key in list(data):
            if key not in allowed:
                data.pop(key)
        return Decision.model_validate(data)


DISCOVERY_SYSTEM_PROMPT = """You operate an unfamiliar UI to accomplish one goal.
You receive a hybrid observation with ephemeral refs. Refs include nearby text,
frame, element type and geometry; use them, never invent CSS or selectors.

Reply with exactly one JSON object:
- Act: {"kind":"act","intent":"...","action":"click|type|select|extract|dismiss",
  "ref":"cN","input_name":"declared_name or null","literal":null,
  "output":"declared_output or null","checkpoint_text":"visible text expected after action or null",
  "risk":"safe|mutating|irreversible","reason":null}
- Done: {"kind":"done","intent":"goal visibly satisfied", ... all other optional fields null}
- Stuck: {"kind":"stuck","intent":"", "reason":"why", ...}

Rules:
- To type, use input_name from declared_inputs, never the value itself. A
  type/select action without input_name or a non-sensitive literal is rejected.
- To extract, identify the ref holding the visible value and set output to the
  declared output *name* (e.g. holds_count), never to the visible value itself.
- Never repeat or expose secret values. Input values are intentionally hidden;
  has_value=true means a field is already filled.
- Include checkpoint_text for any click that changes screens.
- Treat the current visible UI as authoritative over what you expected to see.
- If feedback says the UI did not change, choose the next required field or a
  different action instead of repeating the same one.
- Do not declare done until every required output has been extracted via
  action=extract. A done object must not invent output fields or values.

When target_guidance is present it carries product-specific notes: a preferred
frame to work in, and screen-by-screen expectations. Follow it, but defer to the
visible observation whenever the two disagree."""


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
    ) -> None:
        self.surface = surface
        self.model = model
        self.policy = policy
        self.max_steps = max_steps
        self.event = event_sink
        self.confirmed_risks = confirmed_risks or set()

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
                if checkpoint is not None:
                    await self.surface.checkpoint(checkpoint)
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

