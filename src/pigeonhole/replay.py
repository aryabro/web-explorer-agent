from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pigeonhole.contracts import (
    BusinessOutcome,
    Capability,
    EscalatedResult,
    FailureCode,
    FailureResult,
    FatalState,
    InputValue,
    LiteralValue,
    LocatorVoteRecord,
    OutcomeResult,
    ReplayResult,
    Step,
    StepDiagnostic,
    SuccessResult,
)
from pigeonhole.evidence import EvidenceWriter
from pigeonhole.policy import PolicyEngine
from pigeonhole.surface.base import SurfaceDriver, SurfaceResolutionError


class ReplayEngine:
    """Deterministic executor. This module deliberately has no LLM import."""

    def __init__(
        self,
        *,
        surface: SurfaceDriver,
        policy: PolicyEngine,
        evidence: EvidenceWriter,
        handoff: Any | None = None,
        allow_mutating: bool = False,
        allow_draft: bool = False,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.handoff = handoff
        self.allow_mutating = allow_mutating
        self.allow_draft = allow_draft

    async def run(
        self,
        capability: Capability,
        inputs: dict[str, Any],
        *,
        start_index: int = 0,
        navigate: bool = True,
    ) -> ReplayResult:
        votes: list[LocatorVoteRecord] = []
        missing = [
            name
            for name, definition in capability.contract.inputs.items()
            if definition.required and name not in inputs
        ]
        if missing:
            return await self._failure(
                StepDiagnostic(
                    code=FailureCode.INPUT_INVALID,
                    message=f"missing required inputs: {', '.join(missing)}",
                ),
                [],
                inputs,
                votes,
            )
        approval = capability.governance.approval.status
        if approval == "deprecated":
            return await self._failure(
                StepDiagnostic(
                    code=FailureCode.APPROVAL_REQUIRED,
                    message="deprecated capabilities cannot be replayed",
                ),
                [],
                inputs,
                votes,
            )
        if approval != "approved" and not self.allow_draft:
            return await self._failure(
                StepDiagnostic(
                    code=FailureCode.APPROVAL_REQUIRED,
                    message="draft capabilities require --allow-draft or approval",
                ),
                [],
                inputs,
                votes,
            )
        if len(capability.execution.steps) > self.policy.document["max_steps"]:
            return await self._failure(
                StepDiagnostic(
                    code=FailureCode.POLICY_DENIED,
                    message="capability exceeds the configured step budget",
                ),
                [],
                inputs,
                votes,
            )
        if navigate:
            await self.surface.act(
                "navigate", value=capability.compatibility.surface.entry_point
            )
        completed: list[str] = []
        outputs: dict[str, Any] = {}

        for step in capability.execution.steps[start_index:]:
            observation = await self.surface.observe()
            await self.evidence.event(
                "observed",
                {
                    "step_id": step.id,
                    "url": observation.url,
                    "digest": observation.digest,
                    "control_count": len(observation.controls),
                },
            )
            outcome = await self._detect_outcome(capability)
            if outcome:
                return await self._outcome(outcome, completed, votes)
            fatal = await self._detect_fatal(capability)
            if fatal:
                return await self._escalate_or_fail(
                    self._fatal_diagnostic(step.id, fatal),
                    completed,
                    inputs,
                    capability,
                    votes,
                )

            decision = self.policy.evaluate(
                url=observation.url,
                action=step.action.type,
                risk=step.risk,
                intent=step.intent,
            )
            await self.evidence.event(
                "policy_evaluated",
                {"step_id": step.id, **decision.model_dump(mode="json")},
            )
            if decision.disposition == "deny":
                return await self._failure(
                    StepDiagnostic(
                        step_id=step.id,
                        code=FailureCode.POLICY_DENIED,
                        message=decision.reason,
                    ),
                    completed,
                    inputs,
                    votes,
                )
            if decision.disposition == "confirm" and not self.allow_mutating:
                return await self._escalate_or_fail(
                    StepDiagnostic(
                        step_id=step.id,
                        code=FailureCode.POLICY_REQUIRES_CONFIRMATION,
                        message=decision.reason,
                        expected=step.intent,
                    ),
                    completed,
                    inputs,
                    capability,
                    votes,
                )

            value: Any = None
            if isinstance(step.action.value, InputValue):
                value = inputs[step.action.value.name]
            elif isinstance(step.action.value, LiteralValue):
                value = step.action.value.value

            try:
                if self.handoff is not None:
                    await self.handoff.lease.assert_automation()
                if step.action.type == "navigate":
                    await self.surface.act("navigate", value=value)
                    extracted = None
                elif step.action.type == "wait":
                    await self.surface.act("wait", value=value)
                    extracted = None
                elif step.target is not None:
                    extracted = await self.surface.act_target(
                        step.action.type, step.target, value
                    )
                    vote = self._record_vote(step, capability)
                    if vote is not None:
                        votes.append(vote)
                        await self.evidence.event("locator_vote", vote.model_dump())
                else:
                    raise RuntimeError("targeted action has no target bundle")
            except Exception as exc:
                return await self._escalate_or_fail(
                    StepDiagnostic(
                        step_id=step.id,
                        code=self._action_failure_code(exc),
                        message=f"{type(exc).__name__}: {exc}",
                        expected=step.intent,
                    ),
                    completed,
                    inputs,
                    capability,
                    votes,
                )

            if step.action.output:
                outputs[step.action.output] = extracted
            completed.append(step.id)
            await self.evidence.event(
                "acted",
                {
                    "step_id": step.id,
                    "intent": step.intent,
                    "action": step.action.type,
                    "output_recorded": step.action.output,
                },
            )

            settled = await self._settle_step(
                capability, step, completed, votes, inputs
            )
            if settled is not None:
                return settled

        missing_outputs = set(capability.execution.success.required_outputs) - set(
            outputs
        )
        if missing_outputs:
            return await self._failure(
                StepDiagnostic(
                    code=FailureCode.OUTPUT_MISSING,
                    message=f"required outputs were not extracted: {sorted(missing_outputs)}",
                ),
                completed,
                inputs,
                votes,
            )
        for checkpoint_id in capability.execution.success.checkpoint_ids:
            checkpoint = next(
                (
                    step.checkpoint
                    for step in capability.execution.steps
                    if step.checkpoint and step.checkpoint.id == checkpoint_id
                ),
                None,
            )
            if checkpoint and not await self.surface.checkpoint(checkpoint):
                return await self._failure(
                    StepDiagnostic(
                        code=FailureCode.SUCCESS_CONDITION_FAILED,
                        message="final visible UI condition is no longer present",
                        expected=checkpoint.expected,
                    ),
                    completed,
                    inputs,
                    votes,
                )
        drift = self._drift_score(votes)
        override = self._override_score(votes)
        result = SuccessResult(
            outputs=outputs,
            completed_steps=completed,
            locator_votes=votes,
            override_score=override,
            drift_score=drift,
        )
        await self.evidence.event("succeeded", result.model_dump(mode="json"))
        self.evidence.write_json("result.json", result)
        return result

    async def resume_index(self, capability: Capability) -> int:
        """Infer progress from live UI state, never from a stored step counter."""
        for index in range(len(capability.execution.steps) - 1, -1, -1):
            checkpoint = capability.execution.steps[index].checkpoint
            if checkpoint and await self.surface.checkpoint_visible(checkpoint):
                return index + 1
        return 0

    async def continue_after_handoff(
        self,
        capability: Capability,
        inputs: dict[str, Any],
    ) -> ReplayResult:
        """Resume after an operator returns the live session.

        Progress is derived from visible checkpoints on the current page, not
        from EscalatedResult.completed_steps or any stored cursor.
        """
        if self.handoff is None:
            raise RuntimeError("cannot resume without a handoff coordinator")
        await self.handoff.lease.assert_automation()
        start_index = await self.resume_index(capability)
        await self.evidence.event(
            "resumed",
            {
                "resume_index": start_index,
                "basis": "live checkpoints",
                "holder": "automation",
            },
        )
        return await self.run(
            capability, inputs, start_index=start_index, navigate=False
        )

    async def _settle_step(
        self,
        capability: Capability,
        step: Step,
        completed: list[str],
        votes: list[LocatorVoteRecord],
        inputs: dict[str, Any],
    ) -> ReplayResult | None:
        """Wait for the step checkpoint without missing a declared outcome."""
        timeout_ms = step.checkpoint.timeout_ms if step.checkpoint else 0
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        attempts = [0] * len(step.recover)
        while True:
            await self.surface.observe()
            outcome = await self._detect_outcome(capability)
            if outcome:
                return await self._outcome(outcome, completed, votes)
            fatal = await self._detect_fatal(capability)
            if fatal:
                return await self._escalate_or_fail(
                    self._fatal_diagnostic(step.id, fatal),
                    completed,
                    inputs,
                    capability,
                    votes,
                )
            if step.checkpoint is None:
                return None
            if await self.surface.checkpoint_visible(step.checkpoint):
                await self.evidence.event(
                    "checkpoint",
                    {"step_id": step.id, "checkpoint_id": step.checkpoint.id},
                )
                return None
            recovered = False
            for index, recovery in enumerate(step.recover):
                if attempts[index] >= recovery.max_attempts:
                    continue
                if not await self.surface.checkpoint_visible(recovery.trigger):
                    continue
                if await self.surface.recover(recovery):
                    attempts[index] += 1
                    recovered = True
                    await self.evidence.event(
                        "recovery",
                        {
                            "step_id": step.id,
                            "strategy": recovery.strategy,
                            "attempt": attempts[index],
                            "max_attempts": recovery.max_attempts,
                            "trigger": recovery.trigger.expected,
                        },
                    )
                    deadline = (
                        asyncio.get_running_loop().time()
                        + max(step.checkpoint.timeout_ms, recovery.timeout_ms) / 1000
                    )
                    break
            if recovered:
                continue
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.1)
        observed = (await self.surface.observe()).visible_text[-1000:]
        outcome = await self._detect_outcome(capability)
        if outcome:
            return await self._outcome(outcome, completed, votes)
        assert step.checkpoint is not None
        return await self._escalate_or_fail(
            StepDiagnostic(
                step_id=step.id,
                code=FailureCode.CHECKPOINT_FAILED,
                message="visible UI did not reach the recorded checkpoint",
                expected=step.checkpoint.expected,
                observed=observed,
            ),
            completed,
            inputs,
            capability,
            votes,
        )

    def _record_vote(self, step: Step, capability: Capability) -> LocatorVoteRecord | None:
        vote = getattr(self.surface, "last_vote", None)
        if vote is None:
            return None
        overridden = step.id in capability.compatibility.tenant_overrides.targets
        if (
            step.checkpoint
            and step.checkpoint.id
            in capability.compatibility.tenant_overrides.checkpoints
        ):
            overridden = True
        had_semantic = any(
            strategy.kind == "semantic"
            for strategy in (step.target.strategies if step.target else [])
        )
        drifted = (
            not overridden
            and had_semantic
            and "semantic" not in vote.winners
        )
        return LocatorVoteRecord(
            step_id=step.id,
            winners=list(vote.winners),
            agreement=vote.agreement,
            weak=vote.weak,
            overridden=overridden,
            drifted=drifted,
        )

    @staticmethod
    def _score(votes: list[LocatorVoteRecord], attr: str) -> float:
        if not votes:
            return 0.0
        return round(sum(1 for vote in votes if getattr(vote, attr)) / len(votes), 4)

    @classmethod
    def _drift_score(cls, votes: list[LocatorVoteRecord]) -> float:
        return cls._score(votes, "drifted")

    @classmethod
    def _override_score(cls, votes: list[LocatorVoteRecord]) -> float:
        return cls._score(votes, "overridden")

    async def _outcome(
        self,
        outcome: BusinessOutcome,
        completed: list[str],
        votes: list[LocatorVoteRecord],
    ) -> OutcomeResult:
        result = OutcomeResult(
            code=outcome.code,
            message=outcome.description,
            completed_steps=completed,
            locator_votes=votes,
            override_score=self._override_score(votes),
            drift_score=self._drift_score(votes),
        )
        await self.evidence.event("business_outcome", result.model_dump(mode="json"))
        self.evidence.write_json("result.json", result)
        return result

    async def _detect_outcome(self, capability: Capability) -> BusinessOutcome | None:
        for outcome in capability.contract.business_outcomes:
            if await self.surface.checkpoint_visible(outcome.checkpoint):
                return outcome
        return None

    async def _detect_fatal(self, capability: Capability) -> FatalState | None:
        for state in capability.execution.fatal_states:
            if await self.surface.checkpoint_visible(state.checkpoint):
                return state
        return None

    @staticmethod
    def _failure_code(raw: str) -> FailureCode:
        try:
            return FailureCode(raw)
        except ValueError:
            return FailureCode.ACTION_FAILED

    @staticmethod
    def _action_failure_code(exc: Exception) -> FailureCode:
        if isinstance(exc, SurfaceResolutionError):
            if exc.conflict:
                return FailureCode.LOCATOR_CONFLICT
            return FailureCode.LOCATOR_UNRESOLVED
        return FailureCode.ACTION_FAILED

    @classmethod
    def _fatal_diagnostic(cls, step_id: str, fatal: FatalState) -> StepDiagnostic:
        return StepDiagnostic(
            step_id=step_id,
            code=cls._failure_code(fatal.code),
            message=fatal.description,
            expected=fatal.checkpoint.expected,
            observed=fatal.checkpoint.expected,
        )

    async def _failure(
        self,
        diagnostic: StepDiagnostic,
        completed: list[str],
        inputs: dict[str, Any],
        votes: list[LocatorVoteRecord],
    ) -> FailureResult:
        result = FailureResult(
            error=diagnostic,
            completed_steps=completed,
            locator_votes=votes,
            override_score=self._override_score(votes),
            drift_score=self._drift_score(votes),
        )
        await self.evidence.event("failed", result.model_dump(mode="json"))
        await self.surface.screenshot(
            str(self.evidence.directory / "screenshots" / "failure.png"),
            [str(value) for value in inputs.values()],
        )
        self.evidence.write_json("result.json", result)
        return result

    async def _escalate_or_fail(
        self,
        diagnostic: StepDiagnostic,
        completed: list[str],
        inputs: dict[str, Any],
        capability: Capability,
        votes: list[LocatorVoteRecord],
    ) -> ReplayResult:
        if self.handoff is None:
            return await self._failure(diagnostic, completed, inputs, votes)
        await self.surface.pause()
        await self.evidence.event(
            "lease_ceded",
            {
                "holder": None,
                "step_id": diagnostic.step_id,
                "code": diagnostic.code,
            },
        )
        intervention = await self.handoff.raise_intervention(
            capability_id=capability.contract.id,
            goal=capability.contract.description,
            diagnostic=self.evidence.redactor.data(
                diagnostic.model_dump(mode="json", exclude_none=True)
            ),
            evidence_directory=self.evidence.directory,
            redact_values=[str(value) for value in inputs.values()],
            step_id=diagnostic.step_id,
        )
        result = EscalatedResult(
            intervention_id=intervention.id,
            reason=diagnostic,
            completed_steps=completed,
            locator_votes=votes,
            override_score=self._override_score(votes),
            drift_score=self._drift_score(votes),
        )
        await self.evidence.event("escalated", result.model_dump(mode="json"))
        self.evidence.write_json("result.json", result)
        return result


def load_capability(path: str | Path) -> Capability:
    return Capability.model_validate_json(Path(path).read_text(encoding="utf-8"))
