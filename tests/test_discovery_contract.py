from __future__ import annotations

import json

import pytest

from pigeonhole.contracts import BoundingBox, Parameter, SemanticTarget, TargetBundle
from pigeonhole.discovery import Decision, DiscoveryLoop
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
