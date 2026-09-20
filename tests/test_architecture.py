from __future__ import annotations

import ast
from pathlib import Path

import pytest
import typer

from web_explorer.cli import _runtime_inputs
from web_explorer.compiler import Job
from web_explorer.contracts import (
    Action,
    Capability,
    EscalatedResult,
    FailureResult,
    InputValue,
    OutcomeResult,
    Parameter,
    Recovery,
    Step,
    SuccessResult,
)
from web_explorer.replay import load_capability
from web_explorer.surface.base import SurfaceResolutionError
from web_explorer.surface.playwright import PlaywrightSurface
from web_explorer.surface.resolution import vote_identities

SOURCE_ROOT = Path("src")


def _import_closure(module: str) -> set[str]:
    """Return imports reachable through local web_explorer modules."""
    pending = [module]
    visited: set[str] = set()
    imported: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        path = SOURCE_ROOT.joinpath(*current.split(".")).with_suffix(".py")
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            imported.update(names)
            pending.extend(name for name in names if name.startswith("web_explorer."))
    return imported


def test_replay_module_has_no_llm_or_discovery_dependency() -> None:
    imported = _import_closure("web_explorer.replay")
    forbidden_prefixes = {
        "web_explorer.config",
        "web_explorer.discovery",
        "web_explorer.scripted_model",
        "openai",
        "httpx",
        "anthropic",
        "google.generativeai",
    }
    violations = {
        name
        for name in imported
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in forbidden_prefixes
        )
    }
    assert not violations

    for result_type in (
        SuccessResult,
        OutcomeResult,
        FailureResult,
        EscalatedResult,
    ):
        field = result_type.model_fields["llm_calls"]
        assert field.default == 0
        assert str(field.annotation) == "typing.Literal[0]"


def test_vote_identities_agreement_and_conflict() -> None:
    agreed = vote_identities(
        [("semantic", "el-a"), ("structural", "el-a"), ("geometry", "el-a")]
    )
    assert agreed.agreement == 3
    assert agreed.winners == ["semantic", "structural", "geometry"]
    assert not agreed.weak
    with pytest.raises(SurfaceResolutionError) as conflict:
        vote_identities([("semantic", "el-a"), ("structural", "el-b")])
    assert conflict.value.conflict
    with pytest.raises(SurfaceResolutionError) as unresolved:
        vote_identities([])
    assert not unresolved.value.conflict


def test_recovery_legacy_and_structured_shapes() -> None:
    legacy = Recovery.model_validate(
        {
            "kind": "dismiss_interstitial",
            "visible_text": "NOTICE FROM RECORDS",
            "action_text": "Continue lookup",
        }
    )
    assert legacy.strategy == "dismiss"
    assert legacy.trigger.expected == "NOTICE FROM RECORDS"
    assert legacy.max_attempts == 1
    structured = Recovery.model_validate(
        {
            "trigger": {
                "id": "recover-index",
                "kind": "visible_text",
                "expected": "Index turning...",
            },
            "strategy": "wait",
            "timeout_ms": 3000,
            "max_attempts": 1,
        }
    )
    assert structured.strategy == "wait"
    job = Job.load("jobs/read_savings.yaml")
    assert job.click_recoveries[0].strategy == "dismiss"
    assert job.click_recoveries[1].timeout_ms == 3000


def test_capability_requires_success_outputs_to_be_produced() -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    payload = capability.model_dump(mode="json")
    payload["execution"]["steps"] = [
        step
        for step in payload["execution"]["steps"]
        if step["action"].get("output") != "savings_balance"
    ]
    with pytest.raises(ValueError, match="required outputs are not produced"):
        Capability.model_validate(payload)


def test_capability_requires_declared_required_outputs_on_success() -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    payload = capability.model_dump(mode="json")
    payload["execution"]["success"]["required_outputs"] = []
    with pytest.raises(ValueError, match="absent from the success condition"):
        Capability.model_validate(payload)


def test_step_rejects_internally_invalid_action_shapes() -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    target = capability.execution.steps[0].target
    assert target is not None

    with pytest.raises(ValueError, match="extract action requires an output"):
        Step(id="extract", intent="extract", action=Action(type="extract"), target=target)
    with pytest.raises(ValueError, match="click action cannot write an output"):
        Step(
            id="click",
            intent="click",
            action=Action(type="click", output="savings_balance"),
            target=target,
        )
    with pytest.raises(ValueError, match="type action requires a value"):
        Step(id="type", intent="type", action=Action(type="type"), target=target)
    with pytest.raises(ValueError, match="navigate action requires a value"):
        Step(id="navigate", intent="navigate", action=Action(type="navigate"))
    with pytest.raises(ValueError, match="click action requires a target"):
        Step(id="target", intent="click", action=Action(type="click"))

    valid = Step(
        id="valid",
        intent="type",
        action=Action(type="type", value=InputValue(name="member_id")),
        target=target,
    )
    assert valid.action.type == "type"


def test_cli_parses_declared_number_and_boolean_inputs() -> None:
    definitions = {
        "count": Parameter(type="number", description="count"),
        "enabled": Parameter(type="boolean", description="enabled"),
    }
    assert _runtime_inputs(["count=2.5", "enabled=true"], definitions) == {
        "count": 2.5,
        "enabled": True,
    }
    with pytest.raises(typer.BadParameter, match="unexpected inputs: extra"):
        _runtime_inputs(["count=2", "enabled=false", "extra=no"], definitions)


@pytest.mark.asyncio
async def test_readiness_waits_for_preferred_work_frame_with_generic_fallback() -> None:
    class FakeFrame:
        def __init__(self, name: str, ready_after: int) -> None:
            self.name = name
            self.url = f"http://example.test/{name}"
            self.parent_frame = object()
            self.ready_after = ready_after
            self.calls = 0

        async def evaluate(self, _script: str) -> bool:
            self.calls += 1
            return self.calls >= self.ready_after

    class FakePage:
        def __init__(self, frames: list[FakeFrame]) -> None:
            self.frames = frames

        async def wait_for_load_state(self, *_args, **_kwargs) -> None:
            return None

    navigation = FakeFrame("navigation", ready_after=1)
    work = FakeFrame("work", ready_after=2)
    page = FakePage([navigation, work])
    surface = PlaywrightSurface(
        page,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,
        preferred_frame="work",
    )

    await surface._await_interactive(timeout_ms=500)

    assert work.calls == 2
    assert navigation.calls == 0

    generic = FakeFrame("generic", ready_after=1)
    fallback = PlaywrightSurface(
        FakePage([generic]),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,
        preferred_frame="missing",
    )

    await fallback._await_interactive(timeout_ms=100)

    assert generic.calls == 1
