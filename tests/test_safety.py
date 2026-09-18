from __future__ import annotations

from pathlib import Path

import pytest

from pigeonhole.contracts import BoundingBox, Risk
from pigeonhole.discovery import Decision, DiscoveryLoop
from pigeonhole.evidence import EvidenceWriter
from pigeonhole.handoff import SessionLease
from pigeonhole.policy import PolicyEngine
from pigeonhole.redact import Redactor
from pigeonhole.replay import ExecutionState, ReplayEngine, load_capability
from pigeonhole.surface.base import ControlObservation, Observation
from pigeonhole.tenants import apply_tenant, compatibility_fingerprint, find_profile


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine.load("policy.yaml")


class RecordingSurface:
    def __init__(self, observation: Observation | None = None) -> None:
        self.actions: list[tuple] = []
        self.observation = observation or Observation(
            url="http://127.0.0.1:8765/",
            title="Night Window",
            visible_text="pin 1937 member 12345 Current savings balance: $1842.37",
            controls=[
                ControlObservation(
                    ref="c0",
                    frame="night-work",
                    element_type="button",
                    visible_text="Turn the key",
                    accessible_name="Turn the key",
                    geometry=BoundingBox(x=0, y=0, width=10, height=10),
                ),
                ControlObservation(
                    ref="c1",
                    frame="night-work",
                    element_type="button",
                    visible_text="Confirm and create account",
                    accessible_name="Confirm and create account",
                    geometry=BoundingBox(x=0, y=20, width=10, height=10),
                ),
            ],
            digest="obs-1",
        )

    async def act(self, action: str, ref: str | None = None, value=None):
        self.actions.append((action, ref, value))
        return None

    async def observe(self) -> Observation:
        return self.observation

    async def harvest(self, ref: str):
        raise AssertionError("harvest should not run in this safety test")

    async def resolve(self, target):
        raise AssertionError("resolve should not run in this safety test")

    async def act_target(self, action, target, value=None):
        raise AssertionError("act_target should not run in this safety test")

    async def recover(self, recovery) -> bool:
        return False

    async def checkpoint_visible(self, checkpoint) -> bool:
        return False

    async def checkpoint(self, checkpoint) -> bool:
        return True

    async def screenshot(self, path: str, redact_values=None) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def pause(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def storage_snapshot(self) -> dict:
        return {}

    async def start_human_capture(self) -> None:
        return None

    async def stop_human_capture(self) -> list:
        return []


class CaptureModel:
    name = "safety-model"

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.prompts: list[str] = []

    async def decide(self, prompt: str) -> Decision:
        self.prompts.append(prompt)
        return self.decision


def test_navigate_policy_uses_destination_not_current_url(policy: PolicyEngine) -> None:
    current = "http://127.0.0.1:8765/"
    destination = "https://example.com/steal"
    assert (
        policy.location(action="click", current_url=current, value=destination)
        == current
    )
    assert (
        policy.location(action="navigate", current_url=current, value=destination)
        == destination
    )
    denied = policy.evaluate(
        url=policy.location(
            action="navigate", current_url=current, value=destination
        ),
        action="navigate",
        risk=Risk.SAFE,
    )
    assert denied.disposition == "deny"


def test_trusted_risk_is_never_downgraded_by_the_model(policy: PolicyEngine) -> None:
    mutating_control = ControlObservation(
        ref="c1",
        frame="night-work",
        element_type="button",
        visible_text="Confirm and create account",
        geometry=BoundingBox(x=0, y=0, width=10, height=10),
    )
    sign_on = ControlObservation(
        ref="c0",
        frame="night-work",
        element_type="button",
        visible_text="Turn the key",
        geometry=BoundingBox(x=0, y=0, width=10, height=10),
    )
    assert (
        policy.infer_risk(action="click", control=mutating_control) == Risk.MUTATING
    )
    assert policy.infer_risk(action="click", control=sign_on) == Risk.SAFE


@pytest.mark.asyncio
async def test_discovery_does_not_navigate_before_policy_allow(
    policy: PolicyEngine,
) -> None:
    surface = RecordingSurface()
    result = await DiscoveryLoop(
        surface,
        CaptureModel(Decision(kind="stuck", reason="unused")),
        policy,
    ).run(
        goal="leave the allowlist",
        target="https://example.com/",
        inputs={},
        input_names=[],
        required_outputs=[],
    )
    assert surface.actions == []
    assert result.status == "stopped"
    assert result.recording.stop_reason == "policy_deny"
    assert result.llm_calls == 0


@pytest.mark.asyncio
async def test_model_cannot_downgrade_mutating_action(
    policy: PolicyEngine,
) -> None:
    surface = RecordingSurface()
    model = CaptureModel(
        Decision(
            kind="act",
            intent="Create the account",
            action="click",
            ref="c1",
            checkpoint_text="ACCOUNT CARD CREATED",
            risk=Risk.SAFE,
        )
    )
    result = await DiscoveryLoop(surface, model, policy, max_steps=2).run(
        goal="open an account",
        target="http://127.0.0.1:8765/",
        inputs={},
        input_names=[],
        required_outputs=[],
    )
    assert ("click", "c1", None) not in surface.actions
    assert not any(step.action.type == "click" for step in result.recording.steps)


@pytest.mark.asyncio
async def test_secrets_do_not_reach_model_prompt(
    policy: PolicyEngine, tmp_path: Path
) -> None:
    surface = RecordingSurface()
    model = CaptureModel(Decision(kind="stuck", reason="stop"))
    evidence = EvidenceWriter(
        kind="test-model-egress",
        goal="read",
        target="http://127.0.0.1:8765/",
        model=model.name,
        redactor=Redactor(["1937", "12345", "$1842.37"]),
        root=tmp_path,
    )
    await DiscoveryLoop(
        surface,
        model,
        policy,
        evidence=evidence,
        redact_values=["1937", "12345", "$1842.37"],
    ).run(
        goal="read a balance",
        target="http://127.0.0.1:8765/",
        inputs={"pin": "1937", "member_id": "12345"},
        input_names=["pin", "member_id"],
        required_outputs=["savings_balance"],
    )
    assert model.prompts
    blob = model.prompts[0]
    assert "1937" not in blob
    assert "12345" not in blob
    assert "1842.37" not in blob


@pytest.mark.asyncio
async def test_replay_entry_navigation_is_policy_gated(
    policy: PolicyEngine, tmp_path: Path
) -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    capability.compatibility.surface.entry_point = "https://example.com/"
    surface_contract = capability.compatibility.surface
    capability.compatibility.fingerprint = compatibility_fingerprint(
        vendor=surface_contract.vendor,
        product=surface_contract.product,
        product_version=surface_contract.product_version,
        entry_point=surface_contract.entry_point,
    )
    surface = RecordingSurface()
    evidence = EvidenceWriter(
        kind="test-entry-policy",
        goal=capability.contract.description,
        target=capability.compatibility.surface.entry_point,
        model=None,
        redactor=Redactor([]),
        root=tmp_path,
    )
    result = await ReplayEngine(
        surface=surface, policy=policy, evidence=evidence, allow_draft=True
    ).run(capability, {"operator_id": "teller7", "pin": "1937", "member_id": "12345"})
    assert result.status == "failure"
    assert result.error.code == "POLICY_DENIED"
    assert surface.actions == []


@pytest.mark.asyncio
async def test_expired_lease_never_returns_to_automation() -> None:
    lease = SessionLease()
    await lease.cede()
    await lease.claim("operator", ttl_seconds=0)
    state = await lease.state()
    assert state.holder is None
    with pytest.raises(PermissionError):
        await lease.assert_automation()


@pytest.mark.asyncio
async def test_handoff_cursor_preserves_prior_outputs(
    policy: PolicyEngine, tmp_path: Path
) -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    surface = RecordingSurface()
    evidence = EvidenceWriter(
        kind="test-cursor",
        goal=capability.contract.description,
        target=capability.compatibility.surface.entry_point,
        model=None,
        redactor=Redactor([]),
        root=tmp_path,
    )
    engine = ReplayEngine(
        surface=surface, policy=policy, evidence=evidence, allow_draft=True
    )
    engine._cursor = ExecutionState(
        start_index=3,
        completed=["s1", "s2", "s3"],
        outputs={"prior": "keep-me"},
        votes=[],
    )

    async def visible(checkpoint) -> bool:
        return checkpoint.expected == "MEMBER PROFILE READY"

    surface.checkpoint_visible = visible  # type: ignore[method-assign]
    cursor = await engine._reconcile_cursor(capability)
    assert cursor.start_index == 5
    assert "s5" in cursor.completed
    assert cursor.outputs["prior"] == "keep-me"


def test_tenant_fingerprint_changes_with_origin() -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    specialized = apply_tenant(capability, find_profile("northbay"))
    assert specialized.compatibility.surface.vendor == "north-bay-credit-union"
    assert specialized.compatibility.fingerprint
    assert "tenant-b" in specialized.compatibility.fingerprint
    assert specialized.compatibility.fingerprint != (
        capability.compatibility.fingerprint or ""
    )
