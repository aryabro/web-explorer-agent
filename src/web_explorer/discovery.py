from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from web_explorer.config import LLMConfig
from web_explorer.contracts import (
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
from web_explorer.policy import PolicyEngine
from web_explorer.redact import Redactor
from web_explorer.surface.base import Observation, SurfaceDriver


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["act", "done", "stuck"]
    intent: str = ""
    action: Literal["click", "type", "select", "extract", "dismiss"] | None = None
    ref: str | None = None
    observation_id: str | None = None
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
    execution_status: Literal["succeeded", "failed", "checkpoint_failed"] = "succeeded"
    result_detail: str | None = None
    checkpoint_verified: bool | None = None
    checkpoint_source: Literal["model", "derived", "operator"] | None = None


class RecentAction(BaseModel):
    action: str
    intent: str
    target: str | None = None
    result: str
    checkpoint: str | None = None


class DiscoveryBudget(BaseModel):
    step: int
    max_steps: int
    steps_remaining: int
    seconds_remaining: int


class ModelTurn(BaseModel):
    """Typed, redaction-ready context sent to the discovery model."""

    goal: str
    target_guidance: dict[str, Any] | None = None
    declared_inputs: dict[str, Any]
    required_outputs: dict[str, Any]
    terminal_states: list[dict[str, Any]] = Field(default_factory=list)
    outputs_already_extracted: list[str]
    recent_actions: list[RecentAction]
    feedback: str
    budget: DiscoveryBudget
    observation: Observation


class CompletionAssessment(BaseModel):
    complete: bool
    reasons: list[str] = Field(default_factory=list)


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


class CompletionVerifier:
    """Independently verifies completion from recorded and observed evidence."""

    async def assess(
        self,
        *,
        surface: SurfaceDriver,
        steps: list[RecordedStep],
        outputs: dict[str, Any],
        required_outputs: list[str],
        terminal_states: list[Any] | None = None,
    ) -> CompletionAssessment:
        reasons: list[str] = []
        missing = sorted(set(required_outputs) - set(outputs))
        if missing:
            reasons.append(f"missing required outputs: {missing}")
        unsafe = [step.id for step in steps if step.execution_status != "succeeded"]
        if unsafe:
            reasons.append(f"actions without verified success: {unsafe}")
        transitions = [
            step
            for step in steps
            if step.action.type in {"click", "navigate", "dismiss"}
        ]
        unverified = [
            step.id
            for step in transitions
            if step.checkpoint is None or step.checkpoint_verified is not True
        ]
        if unverified:
            reasons.append(
                f"screen transitions without verified checkpoints: {unverified}"
            )
        checkpoints = [step.checkpoint for step in steps if step.checkpoint is not None]
        if checkpoints and not await surface.checkpoint_visible(checkpoints[-1]):
            reasons.append("the final verified checkpoint is no longer visible")
        for state in terminal_states or []:
            checkpoint = getattr(state, "checkpoint", None)
            if checkpoint is not None and await surface.checkpoint_visible(checkpoint):
                reasons.append(
                    f"terminal state is visible: {getattr(state, 'code', checkpoint.id)}"
                )
        return CompletionAssessment(complete=not reasons, reasons=reasons)


class DecisionModel(Protocol):
    name: str

    async def decide(self, prompt: str) -> Decision: ...


class OpenAICompatibleModel:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.name = config.model
        self.last_metadata: dict[str, Any] = {}

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
        request_started = monotonic()
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
        response_data = response.json()
        self.last_metadata = {
            "request_id": response_data.get("id"),
            "model": response_data.get("model", self.name),
            "usage": response_data.get("usage"),
            "latency_ms": round((monotonic() - request_started) * 1000),
        }
        message = response_data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            raise RuntimeError("model did not return a tool call")
        call = calls[0]["function"]
        data = json.loads(call.get("arguments") or "{}")
        if not isinstance(data, dict):
            data = {}
        tool_name = call.get("name") or "stuck"
        if tool_name in {"click", "type", "select", "extract", "dismiss"}:
            data["kind"] = "act"
            data["action"] = tool_name
        else:
            data["kind"] = tool_name
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


_REF_PROPERTIES = {
    "intent": {"type": "string"},
    "ref": {
        "type": "string",
        "description": "Observation-scoped control ref exactly as listed, e.g. o4:c3",
    },
    "observation_id": {
        "type": "string",
        "description": "The current observation_id",
    },
    "risk": {
        "type": "string",
        "enum": ["safe", "mutating", "irreversible"],
    },
}


def _action_tool(
    name: str,
    description: str,
    *,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {**_REF_PROPERTIES, **(properties or {})},
                "required": ["intent", "ref", "observation_id", *(required or [])],
                "additionalProperties": False,
            },
        },
    }


DISCOVERY_TOOLS = [
    _action_tool(
        "click",
        "Click an interactive ref and name the visible destination checkpoint.",
        properties={"checkpoint_text": {"type": "string"}},
        required=["checkpoint_text"],
    ),
    _action_tool(
        "type",
        "Fill a text control from a declared input, never with its secret value.",
        properties={"input_name": {"type": "string"}},
        required=["input_name"],
    ),
    _action_tool(
        "select",
        "Select an option using a declared input.",
        properties={"input_name": {"type": "string"}},
        required=["input_name"],
    ),
    _action_tool(
        "extract",
        "Extract visible text into a declared output.",
        properties={"output": {"type": "string"}},
        required=["output"],
    ),
    _action_tool(
        "dismiss",
        "Dismiss an interstitial and name the visible destination checkpoint.",
        properties={"checkpoint_text": {"type": "string"}},
        required=["checkpoint_text"],
    ),
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
Refs are scoped to exactly one observation (for example o4:c1). Echo the current
observation_id with every action call. A ref from an earlier observation is invalid.
The current observation is bounded, frame-aware, and authoritative.
Each listed ref includes nearby text, frame, element type and geometry. Choose a
ref from the current observation.
Never invent CSS, selectors, or refs that are not listed.

Call exactly one tool:
- click, type, select, extract, or dismiss: act on a listed ref
- done: only after every required output was extracted
- stuck: the UI cannot be progressed safely

Rules:
- To type, set input_name to a declared input; never send the secret value.
- To extract, set output to a declared output name, never the visible value.
- has_value=true means a field is already filled; do not type it again.
- Respect disabled, required, checked, expanded, dialog, alert, and busy state.
- For click or dismiss, set checkpoint_text to visible destination text.
- Treat the current observation as authoritative.
- Action history is semantic and ref-free; never copy a ref from history.
- You propose actions; the runtime independently verifies action and task success.
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
        policy: PolicyEngine,
        *,
        max_steps: int = 20,
        timeout_seconds: int = 300,
        max_no_progress_steps: int = 3,
        event_sink: EventSink = _noop_event,
        confirmed_risks: set[Risk] | None = None,
        handoff: Any | None = None,
        evidence: Any | None = None,
        on_intervention: Callable[[Any], Awaitable[bool]] | None = None,
        capability_id: str = "discovery",
        redact_values: list[str] | None = None,
        redactor: Redactor | None = None,
        completion_verifier: CompletionVerifier | None = None,
    ) -> None:
        self.surface = surface
        self.model = model
        self.policy = policy
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.max_no_progress_steps = max_no_progress_steps
        self.event = event_sink
        self.confirmed_risks = confirmed_risks or set()
        self.handoff = handoff
        self.evidence = evidence
        self.on_intervention = on_intervention
        self.capability_id = capability_id
        self.redact_values = redact_values or []
        self.redactor = (
            redactor
            or getattr(evidence, "redactor", None)
            or Redactor(self.redact_values)
        )
        self.completion_verifier = completion_verifier or CompletionVerifier()

    async def run(
        self,
        *,
        goal: str,
        target: str,
        inputs: dict[str, Any],
        input_names: list[str],
        required_outputs: list[str],
        input_definitions: dict[str, Any] | None = None,
        output_definitions: dict[str, Any] | None = None,
        business_outcomes: list[Any] | None = None,
        fatal_states: list[Any] | None = None,
        hints: list[str] | None = None,
        preferred_frame: str | None = None,
        click_recoveries: list[Recovery] | None = None,
    ) -> DiscoveryResult:
        started = datetime.now(UTC)
        deadline = monotonic() + self.timeout_seconds
        terminal_checkpoint_texts = {
            state.checkpoint.expected.casefold()
            for state in [*(business_outcomes or []), *(fatal_states or [])]
            if getattr(state, "checkpoint", None) is not None
        }
        guidance: dict[str, Any] = {}
        if preferred_frame:
            guidance["preferred_frame"] = preferred_frame
        if hints:
            guidance["notes"] = list(hints)
        entry = self.policy.evaluate(
            url=target,
            action="navigate",
            risk=Risk.SAFE,
            intent="open discovery target",
        )
        await self.event(
            "policy_evaluated",
            {**entry.model_dump(mode="json"), "phase": "entry", "url": target},
        )
        if entry.disposition != "allow":
            completed = datetime.now(UTC)
            recording = Recording(
                goal=goal,
                target=target,
                model=self.model.name,
                started_at=started,
                completed_at=completed,
                steps=[],
                output_names=required_outputs,
                success=False,
                stop_reason=f"policy_{entry.disposition}",
            )
            return DiscoveryResult(
                status="stopped",
                recording=recording,
                outputs={},
                llm_calls=0,
            )
        await self.surface.act("navigate", value=target)
        steps: list[RecordedStep] = []
        outputs: dict[str, Any] = {}
        llm_calls = 0
        stop_reason = "max_steps"
        success = False
        stagnant_actions = 0
        feedback = ""

        for index in range(self.max_steps):
            if monotonic() >= deadline:
                stop_reason = "timeout"
                break
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

            def _definition(value: Any) -> Any:
                return (
                    value.model_dump(mode="json", exclude_none=True)
                    if hasattr(value, "model_dump")
                    else value
                )

            turn = ModelTurn(
                goal=goal,
                target_guidance=guidance or None,
                declared_inputs={
                    name: _definition(
                        (input_definitions or {}).get(name, {"type": "string"})
                    )
                    for name in input_names
                },
                required_outputs={
                    name: _definition(
                        (output_definitions or {}).get(name, {"type": "string"})
                    )
                    for name in required_outputs
                },
                terminal_states=[
                    _definition(state)
                    for state in [*(business_outcomes or []), *(fatal_states or [])]
                ],
                outputs_already_extracted=list(outputs),
                recent_actions=[
                    RecentAction(
                        action=step.action.type,
                        intent=step.intent,
                        target=step.target.description if step.target else None,
                        result=step.execution_status,
                        checkpoint=step.checkpoint.expected
                        if step.checkpoint
                        else None,
                    )
                    for step in steps[-8:]
                ],
                feedback=feedback,
                budget=DiscoveryBudget(
                    step=index + 1,
                    max_steps=self.max_steps,
                    steps_remaining=self.max_steps - index - 1,
                    seconds_remaining=max(0, int(deadline - monotonic())),
                ),
                observation=(
                    observation.model_copy(update={"visible_text": ""})
                    if observation.frames
                    else observation
                ),
            )
            prompt = json.dumps(
                self.redactor.for_model(turn.model_dump(mode="json", exclude_none=True))
            )
            llm_calls += 1
            try:
                decision = await asyncio.wait_for(
                    self.model.decide(prompt),
                    timeout=max(0.1, min(90.0, deadline - monotonic())),
                )
            except TimeoutError:
                stop_reason = "model_timeout"
                await self.event(
                    "model_failed", {"reason": stop_reason, "step": index + 1}
                )
                break
            except Exception as exc:
                stop_reason = "model_error"
                await self.event(
                    "model_failed",
                    {
                        "reason": stop_reason,
                        "step": index + 1,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                break
            await self.event(
                "model_decided",
                {
                    "kind": decision.kind,
                    "intent": decision.intent,
                    "action": decision.action,
                    "ref": decision.ref,
                    "input_name": decision.input_name,
                    "output": decision.output,
                    "checkpoint_text": decision.checkpoint_text,
                    "metadata": getattr(self.model, "last_metadata", None),
                },
            )

            if decision.kind == "done":
                assessment = await self.completion_verifier.assess(
                    surface=self.surface,
                    steps=steps,
                    outputs=outputs,
                    required_outputs=required_outputs,
                    terminal_states=[
                        *(business_outcomes or []),
                        *(fatal_states or []),
                    ],
                )
                await self.event("completion_verified", assessment.model_dump())
                if not assessment.complete:
                    feedback = "Completion rejected: " + "; ".join(assessment.reasons)
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
                if (
                    decision.observation_id is not None
                    and decision.observation_id != observation.observation_id
                ):
                    feedback = (
                        f"Stale observation {decision.observation_id}. Use only refs from "
                        f"current observation {observation.observation_id}."
                    )
                    continue
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
            if decision.action in {"click", "dismiss"} and not decision.checkpoint_text:
                feedback = (
                    f"{decision.action} requires checkpoint_text describing "
                    "the visible destination state."
                )
                continue
            if (
                decision.action in {"click", "dismiss"}
                and decision.ref
                and decision.checkpoint_text
            ):
                clicked = next(
                    (
                        control
                        for control in observation.controls
                        if control.ref == decision.ref
                    ),
                    None,
                )
                labels = (
                    {
                        (clicked.visible_text or "").strip(),
                        (clicked.accessible_name or "").strip(),
                    }
                    if clicked
                    else set()
                )
                if decision.checkpoint_text.strip() in labels:
                    feedback = (
                        "checkpoint_text must describe the destination screen, "
                        "not the control you just clicked."
                    )
                    continue
            control = next(
                (item for item in observation.controls if item.ref == decision.ref),
                None,
            )
            if control is not None and control.disabled:
                feedback = f"Ref {decision.ref} is disabled in the current observation."
                continue
            if (
                control is not None
                and decision.action in {"click", "dismiss"}
                and not control.interactive
            ):
                feedback = f"Ref {decision.ref} is not an interactive control."
                continue
            if (
                control is not None
                and decision.action == "type"
                and control.element_type not in {"input", "textarea"}
            ):
                feedback = f"Ref {decision.ref} is not a text-entry control."
                continue
            if (
                control is not None
                and decision.action == "select"
                and control.element_type != "select"
            ):
                feedback = f"Ref {decision.ref} is not a select control."
                continue
            inferred = self.policy.infer_risk(action=decision.action, control=control)
            await self.event(
                "risk_inferred",
                {
                    "proposed": decision.risk,
                    "inferred": inferred,
                    "advisory_only": True,
                },
            )
            policy_url = self.policy.location(
                action=decision.action,
                current_url=observation.url,
                value=None,
            )
            policy_decision = self.policy.evaluate(
                url=policy_url,
                action=decision.action,
                risk=inferred,
                intent=decision.intent,
            )
            await self.event(
                "policy_evaluated", policy_decision.model_dump(mode="json")
            )
            if (
                policy_decision.disposition == "confirm"
                and inferred in self.confirmed_risks
            ):
                await self.event(
                    "human_confirmed",
                    {"risk": inferred, "intent": decision.intent},
                )
            elif policy_decision.disposition != "allow":
                feedback = (
                    f"Policy {policy_decision.disposition}: {policy_decision.reason}"
                )
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
            if target_bundle is not None:
                safe_strategies = [
                    strategy
                    for strategy in target_bundle.strategies
                    if self.redactor.data(strategy.model_dump(mode="json"))
                    == strategy.model_dump(mode="json")
                ]
                if not safe_strategies:
                    feedback = (
                        "The selected control has no persistable target strategy after "
                        "sensitive-data filtering. Choose a label-based control."
                    )
                    await self.event("target_rejected", {"reason": feedback})
                    continue
                if len(safe_strategies) != len(target_bundle.strategies):
                    await self.event(
                        "target_sanitized",
                        {
                            "removed_strategies": len(target_bundle.strategies)
                            - len(safe_strategies)
                        },
                    )
                target_bundle = TargetBundle(
                    description=self.redactor.text(target_bundle.description),
                    strategies=safe_strategies,
                )
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
            extracted = None
            execution_status: Literal["succeeded", "failed", "checkpoint_failed"] = (
                "succeeded"
            )
            result_detail: str | None = None
            checkpoint_verified: bool | None = None
            checkpoint_source: Literal["model", "derived", "operator"] | None = (
                "model" if checkpoint is not None else None
            )
            try:
                extracted = await self.surface.act(
                    decision.action, decision.ref, runtime_value
                )
                if checkpoint is not None:
                    checkpoint_verified = await self.surface.checkpoint(checkpoint)
                    if not checkpoint_verified:
                        execution_status = "checkpoint_failed"
                        result_detail = (
                            "Destination checkpoint was not visible after the action."
                        )
            except Exception as exc:
                execution_status = "failed"
                result_detail = f"{type(exc).__name__}: {exc}"

            # An attempted action is evidence even when its postcondition fails.
            # Recording it prevents a later `done` call from compiling a fiction.
            try:
                after = await self.surface.observe()
            except Exception:
                after = observation
            proposed_checkpoint = checkpoint
            if (
                execution_status == "checkpoint_failed"
                and checkpoint is not None
                and after.digest != observation.digest
            ):
                derived = await self._derive_checkpoint(
                    before=observation,
                    after=after,
                    checkpoint_id=checkpoint.id,
                    forbidden=terminal_checkpoint_texts,
                )
                if derived is not None:
                    checkpoint = derived
                    checkpoint_verified = True
                    checkpoint_source = "derived"
                    execution_status = "succeeded"
                    result_detail = None
                    await self.event(
                        "checkpoint_recovered",
                        {
                            "source": "derived",
                            "proposed": proposed_checkpoint.expected,
                            "verified": derived.expected,
                            "frame": derived.frame,
                        },
                    )
            if execution_status == "succeeded" and after.url != observation.url:
                redirect_policy = self.policy.evaluate(
                    url=after.url,
                    action="navigate",
                    risk=Risk.SAFE,
                    intent="validate action destination",
                )
                await self.event(
                    "policy_evaluated",
                    {
                        **redirect_policy.model_dump(mode="json"),
                        "phase": "post_action_destination",
                        "url": after.url,
                    },
                )
                if redirect_policy.disposition != "allow":
                    execution_status = "failed"
                    result_detail = (
                        "Action navigated outside policy: " + redirect_policy.reason
                    )
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
                    risk=inferred,
                    before_digest=observation.digest,
                    after_digest=after.digest,
                    execution_status=execution_status,
                    result_detail=result_detail,
                    checkpoint_verified=checkpoint_verified,
                    checkpoint_source=checkpoint_source,
                )
            )
            await self.event(
                "acted",
                {
                    "step_id": steps[-1].id,
                    "action": decision.action,
                    "after_digest": after.digest,
                    "execution_status": execution_status,
                    "checkpoint_verified": checkpoint_verified,
                    "checkpoint_text": checkpoint.expected if checkpoint else None,
                    "checkpoint_source": checkpoint_source,
                },
            )
            if execution_status != "succeeded":
                feedback = result_detail or "Action did not reach its postcondition."
                await self.event(
                    "action_failed",
                    {"step_id": steps[-1].id, "error": feedback},
                )
                stop_reason = execution_status
                if await self._intervene(
                    goal=goal,
                    reason=feedback,
                    step_id=steps[-1].id,
                    expected=checkpoint.expected if checkpoint else None,
                    observed=self._observed_summary(after),
                ):
                    reconciled = await self.surface.observe()
                    repaired = await self._reconcile_checkpoint_after_handoff(
                        step=steps[-1],
                        before=observation,
                        after=reconciled,
                        forbidden=terminal_checkpoint_texts,
                    )
                    if repaired:
                        stagnant_actions = 0
                        feedback = (
                            "Operator returned the session and the destination state "
                            "was independently reconciled. Continue from the current UI."
                        )
                        continue
                    stop_reason = "checkpoint_failed_after_handoff"
                    feedback = (
                        "Operator returned the session, but no destination checkpoint "
                        "could be verified."
                    )
                break
            if decision.output:
                outputs[decision.output] = extracted
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
            if stagnant_actions >= self.max_no_progress_steps:
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

        if not success:
            await self._capture_failure_evidence(stop_reason)
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

    def _checkpoint_candidates(
        self, before: Observation, after: Observation
    ) -> list[tuple[str, str | None]]:
        before_text = {
            " ".join(text.split()).casefold()
            for control in before.controls
            for text in (control.visible_text, control.accessible_name or "")
            if text.strip()
        }
        ranked: list[tuple[int, str, str | None]] = []
        seen: set[tuple[str, str | None]] = set()
        for control in after.controls:
            if control.interactive:
                continue
            for raw in (control.visible_text, control.accessible_name or ""):
                text = " ".join(raw.split())
                key = (text.casefold(), control.frame)
                if (
                    not text
                    or len(text) > 160
                    or text.casefold() in before_text
                    or key in seen
                    or self.redactor.text(text) != text
                ):
                    continue
                seen.add(key)
                upper = text.upper() == text and any(char.isalpha() for char in text)
                keyword = any(
                    word in text.upper()
                    for word in ("READY", "SUCCESS", "CONFIRMED", "COMPLETE")
                )
                semantic = control.element_type in {"h1", "h2", "h3", "b", "strong"}
                score = (
                    (100 if keyword else 0)
                    + (40 if upper else 0)
                    + (20 if semantic else 0)
                    - len(text) // 20
                )
                ranked.append((score, text, control.frame))
        ranked.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        return [(text, frame) for _, text, frame in ranked]

    async def _derive_checkpoint(
        self,
        *,
        before: Observation,
        after: Observation,
        checkpoint_id: str,
        forbidden: set[str] | None = None,
    ) -> Checkpoint | None:
        for text, frame in self._checkpoint_candidates(before, after):
            if text.casefold() in (forbidden or set()):
                continue
            candidate = Checkpoint(
                id=checkpoint_id,
                kind="visible_text",
                expected=text,
                frame=frame,
            )
            if await self.surface.checkpoint_visible(candidate):
                return candidate
        return None

    def _observed_summary(self, observation: Observation) -> str:
        candidates = [
            text for text, _ in self._checkpoint_candidates(observation, observation)
        ]
        # The same observation has no delta, so include safe visible landmarks directly.
        if not candidates:
            candidates = [
                " ".join(control.visible_text.split())
                for control in observation.controls
                if not control.interactive
                and control.visible_text.strip()
                and len(control.visible_text) <= 160
                and self.redactor.text(control.visible_text) == control.visible_text
            ][:8]
        return f"digest={observation.digest}; landmarks={candidates[:8]}"

    async def _reconcile_checkpoint_after_handoff(
        self,
        *,
        step: RecordedStep,
        before: Observation,
        after: Observation,
        forbidden: set[str] | None = None,
    ) -> bool:
        checkpoint = step.checkpoint
        if checkpoint is not None and await self.surface.checkpoint_visible(checkpoint):
            repaired = checkpoint
        elif checkpoint is not None:
            repaired = await self._derive_checkpoint(
                before=before,
                after=after,
                checkpoint_id=checkpoint.id,
                forbidden=forbidden,
            )
        else:
            repaired = None
        if repaired is None:
            await self.event(
                "handoff_reconciliation_failed",
                {
                    "step_id": step.id,
                    "observed": self._observed_summary(after),
                },
            )
            return False
        proposed = checkpoint.expected if checkpoint else None
        step.checkpoint = repaired
        step.checkpoint_verified = True
        step.checkpoint_source = "operator"
        step.execution_status = "succeeded"
        step.result_detail = None
        step.after_digest = after.digest
        await self.event(
            "handoff_reconciled",
            {
                "step_id": step.id,
                "proposed": proposed,
                "verified": repaired.expected,
                "frame": repaired.frame,
                "after_digest": after.digest,
            },
        )
        return True

    async def _capture_failure_evidence(self, reason: str) -> None:
        if self.evidence is None or not hasattr(self.evidence, "directory"):
            return
        destination = self.evidence.directory / "screenshots" / "discovery-failure.png"
        try:
            await self.surface.screenshot(
                str(destination), redact_values=self.redact_values
            )
            await self.event(
                "failure_evidence_captured",
                {"reason": reason, "screenshot": str(destination)},
            )
        except Exception as exc:
            await self.event(
                "failure_evidence_failed",
                {"reason": reason, "error_type": type(exc).__name__},
            )

    async def _intervene(
        self,
        *,
        goal: str,
        reason: str,
        step_id: str | None,
        expected: str | None = None,
        observed: str | None = None,
    ) -> bool:
        diagnostic = StepDiagnostic(
            step_id=step_id,
            code=FailureCode.ACTION_FAILED,
            message=reason,
            expected=expected or goal,
            observed=observed,
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
