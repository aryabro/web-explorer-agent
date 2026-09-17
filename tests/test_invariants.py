from __future__ import annotations

import json
from pathlib import Path

import pytest

from pigeonhole.compiler import Job, compile_recording
from pigeonhole.contracts import Action, FailureCode, Step, SuccessCondition, TargetBundle
from pigeonhole.discovery import Decision, DiscoveryLoop
from pigeonhole.evidence import EvidenceWriter
from pigeonhole.fixture_model import ScriptedModel
from pigeonhole.handoff import HandoffCoordinator, SessionLease
from pigeonhole.policy import PolicyEngine
from pigeonhole.redact import Redactor
from pigeonhole.replay import ReplayEngine, load_capability
from pigeonhole.surface.playwright import PlaywrightSurface


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine.load("policy.yaml")


@pytest.mark.asyncio
async def test_lease_ttl_expire_leaves_session_unowned() -> None:
    lease = SessionLease()
    await lease.cede()
    await lease.claim("op-ttl", ttl_seconds=0)
    state = await lease.state()
    assert state.holder is None
    with pytest.raises(PermissionError):
        await lease.assert_automation()


@pytest.mark.asyncio
async def test_locator_disagreement_is_conflict(
    tmp_path: Path, policy: PolicyEngine
) -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    surface = await PlaywrightSurface.launch(headless=True)
    try:
        await surface.act(
            "navigate", value=capability.compatibility.surface.entry_point
        )
        observation = await surface.observe()
        inputs = [
            control.ref
            for control in observation.controls
            if control.element_type == "input"
        ]
        assert len(inputs) >= 2
        first = await surface.harvest(inputs[0])
        second = await surface.harvest(inputs[1])
        conflicting = TargetBundle(
            description="synthetic locator conflict",
            strategies=[first.strategies[0], second.strategies[0]],
        )
        synthetic = capability.model_copy(deep=True)
        synthetic.execution.steps = [
            Step(
                id="conflict",
                intent="Act on a bundle whose strategies disagree",
                action=Action(type="click"),
                target=conflicting,
            )
        ]
        synthetic.execution.success = SuccessCondition(
            checkpoint_ids=[],
            required_outputs=[],
        )
        evidence = EvidenceWriter(
            kind="test-conflict",
            goal=capability.contract.description,
            target=capability.compatibility.surface.entry_point,
            model=None,
            redactor=Redactor(["1937", "12345"]),
            root=tmp_path,
        )
        result = await ReplayEngine(
            surface=surface,
            policy=policy,
            evidence=evidence,
            allow_draft=True,
        ).run(synthetic, {"operator_id": "teller7", "pin": "1937", "member_id": "12345"})
        assert result.status == "failure"
        assert result.error.code == FailureCode.LOCATOR_CONFLICT
    finally:
        await surface.close()


def test_redactor_masks_sinks_not_capability_locators(tmp_path: Path) -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    original = [
        step.target.model_dump(mode="json") if step.target else None
        for step in capability.execution.steps
    ]
    writer = EvidenceWriter(
        kind="test-redact-boundary",
        goal=capability.contract.description,
        target=capability.compatibility.surface.entry_point,
        model=None,
        redactor=Redactor(["1937", "12345", "$1842.37", "teller7"]),
        root=tmp_path,
    )
    writer.write_json(
        "recording.json",
        {
            "secret": "pin=1937 member 12345",
            "steps": [
                {"target": target, "intent": "type 12345"}
                for target in original
                if target is not None
            ],
        },
    )
    dumped = json.loads(
        (writer.directory / "recording.json").read_text(encoding="utf-8")
    )
    assert "1937" not in dumped["secret"]
    assert "12345" not in dumped["secret"]
    written = [step["target"] for step in dumped["steps"]]
    expected = [target for target in original if target is not None]
    assert written == expected
    assert all("12345" not in step["intent"] for step in dumped["steps"])


@pytest.mark.asyncio
async def test_discover_then_replay_different_member(
    tmp_path: Path, policy: PolicyEngine
) -> None:
    job = Job.load("jobs/read_savings.yaml")
    discover_inputs = {
        "operator_id": "teller7",
        "pin": "1937",
        "member_id": "12345",
    }
    surface = await PlaywrightSurface.launch(headless=True)
    try:
        result = await DiscoveryLoop(
            surface, ScriptedModel("read"), policy, max_steps=job.max_steps
        ).run(
            goal=job.goal,
            target=job.target,
            inputs=discover_inputs,
            input_names=list(job.inputs),
            required_outputs=list(job.outputs),
            preferred_frame=job.preferred_frame,
            click_recoveries=job.click_recoveries,
        )
        capability = compile_recording(result.recording, job, trace_ref="test")
        assert result.outputs["savings_balance"] == "$1842.37"
    finally:
        await surface.close()

    replay_surface = await PlaywrightSurface.launch(headless=True)
    replay_inputs = {**discover_inputs, "member_id": "54321"}
    try:
        evidence = EvidenceWriter(
            kind="test-replay-other-member",
            goal=job.goal,
            target=job.target,
            model=None,
            redactor=Redactor(list(replay_inputs.values()) + ["$92.14"]),
            root=tmp_path,
        )
        replay = await ReplayEngine(
            surface=replay_surface,
            policy=policy,
            evidence=evidence,
            allow_draft=True,
        ).run(capability, replay_inputs)
        assert replay.status == "success"
        assert replay.outputs["savings_balance"] == "$92.14"
        assert replay.llm_calls == 0
    finally:
        await replay_surface.close()


@pytest.mark.asyncio
async def test_discovery_stuck_persists_intervention(
    tmp_path: Path, policy: PolicyEngine
) -> None:
    class StuckModel:
        name = "stuck-model"

        async def decide(self, prompt: str) -> Decision:
            return Decision(kind="stuck", reason="cannot progress")

    job = Job.load("jobs/read_savings.yaml")
    surface = await PlaywrightSurface.launch(headless=True)
    coordinator = HandoffCoordinator(surface)
    evidence = EvidenceWriter(
        kind="test-discovery-stuck",
        goal=job.goal,
        target=job.target,
        model=StuckModel.name,
        redactor=Redactor(["1937"]),
        root=tmp_path,
    )
    try:
        result = await DiscoveryLoop(
            surface,
            StuckModel(),
            policy,
            max_steps=3,
            event_sink=evidence.event,
            handoff=coordinator,
            evidence=evidence,
            capability_id=job.capability_id,
            redact_values=["1937"],
        ).run(
            goal=job.goal,
            target=job.target,
            inputs={"operator_id": "teller7", "pin": "1937", "member_id": "12345"},
            input_names=list(job.inputs),
            required_outputs=list(job.outputs),
        )
        assert result.status == "stopped"
        assert result.recording.stop_reason == "cannot progress"
        assert (evidence.directory / "intervention.json").exists()
        assert (await coordinator.lease.state()).holder is None
    finally:
        await surface.close()
