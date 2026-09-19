from __future__ import annotations

import json

import pytest

from pigeonhole.contracts import (
    Action,
    BoundingBox,
    Checkpoint,
    Parameter,
    Risk,
    SemanticTarget,
    TargetBundle,
)
from pigeonhole.discovery import Decision, DiscoveryLoop, RecordedStep
from pigeonhole.policy import PolicyEngine
from pigeonhole.surface.base import ControlObservation, Observation


class ContractSurface:
    def __init__(self) -> None:
        self.clicked = False
        self.actions: list[tuple[str, str | None]] = []

    async def observe(self) -> Observation:
        return Observation(
            observation_id="o2" if self.clicked else "o1",
            url="http://127.0.0.1:8765/",
            title="Test",
            visible_text="Destination" if self.clicked else "Start",
            controls=[
                ControlObservation(
                    ref=("o2:c0" if self.clicked else "o1:c0"),
                    frame="main",
                    element_type="button",
                    role="button",
                    accessible_name="Continue",
                    visible_text="Continue",
                    geometry=BoundingBox(x=0, y=0, width=10, height=10),
                )
            ],
            digest="after" if self.clicked else "before",
        )

    async def act(self, action: str, ref: str | None = None, value=None):
        self.actions.append((action, ref))
        if action == "click":
            self.clicked = True

    async def harvest(self, ref: str) -> TargetBundle:
        return TargetBundle(
            description="Continue",
            strategies=[
                SemanticTarget(
                    frame="main", role="button", name="Continue", element_type="button"
                )
            ],
        )

    async def checkpoint(self, checkpoint) -> bool:
        return False

    async def checkpoint_visible(self, checkpoint) -> bool:
        return False


class OneDecisionModel:
    name = "test-model"

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.prompts: list[str] = []

    async def decide(self, prompt: str) -> Decision:
        self.prompts.append(prompt)
        return self.decision


class SequenceModel:
    name = "test-model"

    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = iter(decisions)

    async def decide(self, prompt: str) -> Decision:
        return next(self.decisions)


class LandmarkSurface(ContractSurface):
    async def observe(self) -> Observation:
        observation = await super().observe()
        if self.clicked:
            observation.controls.append(
                ControlObservation(
                    ref="o2:c1",
                    frame="main",
                    element_type="b",
                    interactive=False,
                    visible_text="DESTINATION READY",
                    geometry=BoundingBox(x=0, y=20, width=80, height=10),
                )
            )
        return observation

    async def checkpoint_visible(self, checkpoint) -> bool:
        return self.clicked and checkpoint.expected == "DESTINATION READY"


@pytest.mark.asyncio
async def test_executed_action_with_failed_checkpoint_is_recorded() -> None:
    surface = ContractSurface()
    model = OneDecisionModel(
        Decision(
            kind="act",
            intent="Continue",
            action="click",
            ref="o1:c0",
            observation_id="o1",
            checkpoint_text="Never appears",
        )
    )
    result = await DiscoveryLoop(surface, model, PolicyEngine.load(), max_steps=2).run(
        goal="continue",
        target="http://127.0.0.1:8765/",
        inputs={},
        input_names=[],
        required_outputs=[],
    )

    assert result.status == "stopped"
    assert result.recording.stop_reason == "checkpoint_failed"
    assert len(result.recording.steps) == 1
    assert result.recording.steps[0].execution_status == "checkpoint_failed"
    assert result.recording.steps[0].checkpoint_verified is False


@pytest.mark.asyncio
async def test_bad_model_checkpoint_is_replaced_by_new_verified_landmark() -> None:
    surface = LandmarkSurface()
    model = SequenceModel(
        [
            Decision(
                kind="act",
                intent="Continue",
                action="click",
                ref="o1:c0",
                observation_id="o1",
                checkpoint_text="A made-up destination description",
            ),
            Decision(kind="done", intent="Destination reached"),
        ]
    )
    result = await DiscoveryLoop(surface, model, PolicyEngine.load(), max_steps=2).run(
        goal="continue",
        target="http://127.0.0.1:8765/",
        inputs={},
        input_names=[],
        required_outputs=[],
    )

    assert result.status == "success"
    step = result.recording.steps[0]
    assert step.execution_status == "succeeded"
    assert step.checkpoint_verified is True
    assert step.checkpoint_source == "derived"
    assert step.checkpoint is not None
    assert step.checkpoint.expected == "DESTINATION READY"


@pytest.mark.asyncio
async def test_handoff_return_can_reconcile_and_repair_failed_step() -> None:
    surface = LandmarkSurface()
    surface.clicked = True
    loop = DiscoveryLoop(
        surface, OneDecisionModel(Decision(kind="done")), PolicyEngine.load()
    )
    before = Observation(
        observation_id="o1",
        url="http://127.0.0.1:8765/",
        title="Test",
        visible_text="Start",
        controls=[],
        digest="before",
    )
    after = await surface.observe()
    step = RecordedStep(
        id="s1",
        intent="Continue",
        action=Action(type="click"),
        target=None,
        checkpoint=Checkpoint(
            id="cp1", kind="visible_text", expected="Wrong checkpoint"
        ),
        risk=Risk.SAFE,
        before_digest="before",
        after_digest=after.digest,
        execution_status="checkpoint_failed",
        checkpoint_verified=False,
        checkpoint_source="model",
    )

    assert await loop._reconcile_checkpoint_after_handoff(
        step=step, before=before, after=after
    )
    assert step.execution_status == "succeeded"
    assert step.checkpoint_source == "operator"
    assert step.checkpoint is not None
    assert step.checkpoint.expected == "DESTINATION READY"


@pytest.mark.asyncio
async def test_stale_observation_id_cannot_act() -> None:
    surface = ContractSurface()
    model = OneDecisionModel(
        Decision(
            kind="act",
            intent="Continue",
            action="click",
            ref="o1:c0",
            observation_id="o0",
            checkpoint_text="Destination",
        )
    )
    result = await DiscoveryLoop(surface, model, PolicyEngine.load(), max_steps=1).run(
        goal="continue",
        target="http://127.0.0.1:8765/",
        inputs={},
        input_names=[],
        required_outputs=[],
    )

    assert ("click", "o1:c0") not in surface.actions
    assert result.recording.steps == []


@pytest.mark.asyncio
async def test_model_turn_contains_typed_contract_and_budget() -> None:
    surface = ContractSurface()
    model = OneDecisionModel(Decision(kind="stuck", reason="done testing"))
    parameter = Parameter(type="string", description="A member identifier")
    await DiscoveryLoop(surface, model, PolicyEngine.load(), max_steps=4).run(
        goal="inspect",
        target="http://127.0.0.1:8765/",
        inputs={"member_id": "12345"},
        input_names=["member_id"],
        required_outputs=["balance"],
        input_definitions={"member_id": parameter},
        output_definitions={"balance": Parameter(type="string", description="Balance")},
    )

    turn = json.loads(model.prompts[0])
    assert turn["declared_inputs"]["member_id"]["description"] == "A member identifier"
    assert turn["required_outputs"]["balance"]["type"] == "string"
    assert turn["budget"]["steps_remaining"] == 3
    assert turn["observation"]["observation_id"] == "o1"
