from __future__ import annotations

import json
from pathlib import Path

import pytest

from pigeonhole.compiler import Job, compile_recording
from pigeonhole.contracts import (
    InputValue,
    LiteralValue,
    Risk,
    Sensitivity,
)
from pigeonhole.discovery import DiscoveryLoop
from pigeonhole.evidence import EvidenceWriter
from pigeonhole.handoff import HandoffCoordinator, SessionLease
from pigeonhole.policy import PolicyEngine
from pigeonhole.redact import Redactor
from pigeonhole.replay import ReplayEngine, load_capability
from pigeonhole.scripted_model import ScriptedModel
from pigeonhole.tenants import apply_tenant, find_profile
from target.profile import launch_browser


async def _compile_read_savings(policy: PolicyEngine):
    inputs = {"operator_id": "teller7", "pin": "1937", "member_id": "12345"}
    job = Job.load("jobs/read_savings.yaml")
    surface = await launch_browser(headless=True)
    try:
        result = await DiscoveryLoop(
            surface, ScriptedModel("read"), policy, max_steps=job.max_steps
        ).run(
            goal=job.goal,
            target=job.target,
            inputs=inputs,
            input_names=list(job.inputs),
            required_outputs=list(job.outputs),
            hints=job.discovery_hints,
            preferred_frame=job.preferred_frame,
            click_recoveries=job.click_recoveries,
        )
        return compile_recording(result.recording, job, trace_ref="test"), inputs
    finally:
        await surface.close()


def test_policy_default_deny_and_risk(policy: PolicyEngine) -> None:
    assert (
        policy.evaluate(
            url="http://127.0.0.1:8765/",
            action="click",
            risk=Risk.SAFE,
        ).disposition
        == "allow"
    )
    assert (
        policy.evaluate(
            url="http://127.0.0.1:8765/signin.html",
            action="click",
            risk=Risk.SAFE,
        ).disposition
        == "allow"
    )
    assert (
        policy.evaluate(
            url="http://127.0.0.1:8765/tenant-b/detail.html",
            action="click",
            risk=Risk.SAFE,
        ).disposition
        == "allow"
    )
    assert (
        policy.evaluate(
            url="http://127.0.0.1:8765/",
            action="click",
            risk=Risk.MUTATING,
        ).disposition
        == "confirm"
    )
    assert (
        policy.evaluate(
            url="http://127.0.0.1:8765/",
            action="click",
            risk=Risk.IRREVERSIBLE,
        ).disposition
        == "deny"
    )
    assert (
        policy.evaluate(
            url="https://example.com/",
            action="click",
            risk=Risk.SAFE,
        ).disposition
        == "deny"
    )
    assert (
        policy.evaluate(
            url="http://127.0.0.1:8765/admin",
            action="click",
            risk=Risk.SAFE,
        ).disposition
        == "deny"
    )
    assert (
        policy.evaluate(
            url="https://www.monroetwplibrary.org/",
            action="click",
            risk=Risk.SAFE,
        ).disposition
        == "deny"
    )


def test_catalog_holds_job_is_loadable() -> None:
    job = Job.load("jobs/catalog_holds.yaml")
    assert job.target == "https://mon.search.stellanj.org/"
    assert "title" in job.inputs
    assert "holds_count" in job.outputs
    assert job.fatal_states == []
    assert job.click_recoveries[0].action_text == "Close"
    assert job.click_recoveries[0].strategy == "dismiss"


def test_secret_definitions_allowed_but_secret_literals_impossible() -> None:
    job = Job.load("jobs/read_savings.yaml")
    assert job.inputs["pin"].sensitivity == Sensitivity.SECRET
    value = InputValue(name="pin")
    assert value.model_dump() == {"source": "input", "name": "pin"}
    with pytest.raises(Exception):
        LiteralValue(value="1937", sensitivity="secret")


def test_redactor_masks_known_and_pattern_values() -> None:
    redactor = Redactor(["1937", "12345"])
    text = redactor.text("member 12345 pin=1937 has $1,842.37")
    assert "12345" not in text
    assert "1937" not in text
    assert "1,842.37" not in text
    assert redactor.text("member 60123 jacket") == "member [REDACTED] jacket"
    stamp = "2026-09-14T22:56:26.474007+00:00"
    assert redactor.text(stamp) == stamp


@pytest.mark.asyncio
async def test_control_lease_is_fail_closed() -> None:
    lease = SessionLease()
    await lease.assert_automation()
    await lease.cede()
    with pytest.raises(PermissionError):
        await lease.assert_automation()
    await lease.claim("op-1", ttl_seconds=60)
    with pytest.raises(RuntimeError):
        await lease.claim("op-2", ttl_seconds=60)
    await lease.hand_back("op-1")
    await lease.assert_automation()


@pytest.mark.asyncio
async def test_discovery_compile_and_replay_real_ui(
    tmp_path: Path, policy: PolicyEngine, test_bank_server
) -> None:
    inputs = {"operator_id": "teller7", "pin": "1937", "member_id": "12345"}
    job = Job.load("jobs/read_savings.yaml")
    discovery_surface = await launch_browser(headless=True)
    try:
        discovery_evidence = EvidenceWriter(
            kind="test-discovery",
            goal=job.goal,
            target=job.target,
            model=ScriptedModel.name,
            redactor=Redactor(["1937", "12345"]),
            root=tmp_path,
        )
        result = await DiscoveryLoop(
            discovery_surface,
            ScriptedModel("read"),
            policy,
            max_steps=job.max_steps,
            event_sink=discovery_evidence.event,
        ).run(
            goal=job.goal,
            target=job.target,
            inputs=inputs,
            input_names=list(job.inputs),
            required_outputs=list(job.outputs),
            hints=job.discovery_hints,
            preferred_frame=job.preferred_frame,
            click_recoveries=job.click_recoveries,
        )
        assert result.status == "success"
        assert result.outputs["savings_balance"] == "$1842.37"
        capability = compile_recording(
            result.recording,
            job,
            trace_ref="test-only/trace.jsonl",
        )
        serialized = capability.model_dump_json()
        assert "1937" not in serialized
        assert "value redacted" not in serialized.lower()
        assert len(capability.execution.steps[0].target.strategies) == 3
        postcondition = capability.execution.success.checkpoint_ids[-1]
        assert any(
            step.checkpoint and step.checkpoint.id == postcondition
            and step.checkpoint.role == "postcondition"
            for step in capability.execution.steps
        )
        assert capability.compatibility.fingerprint
    finally:
        await discovery_surface.close()

    replay_surface = await launch_browser(headless=True)
    try:
        replay_evidence = EvidenceWriter(
            kind="test-replay",
            goal=job.goal,
            target=job.target,
            model=None,
            redactor=Redactor(["1937", "12345", "$1842.37"]),
            root=tmp_path,
        )
        replay = await ReplayEngine(
            surface=replay_surface,
            policy=policy,
            evidence=replay_evidence,
            allow_draft=True,
        ).run(capability, inputs)
        assert replay.status == "success"
        assert replay.outputs["savings_balance"] == "$1842.37"
        assert replay.llm_calls == 0
        assert replay.locator_votes
        assert all(vote.agreement >= 1 for vote in replay.locator_votes)
        assert any("semantic" in vote.winners for vote in replay.locator_votes)
        evidence_text = "".join(
            path.read_text(encoding="utf-8")
            for path in replay_evidence.directory.glob("*.json*")
        )
        assert "1937" not in evidence_text
        assert "12345" not in evidence_text
        assert "$1842.37" not in evidence_text
    finally:
        await replay_surface.close()


@pytest.mark.asyncio
async def test_business_outcome_and_interstitial_recovery(
    tmp_path: Path, policy: PolicyEngine, test_bank_server
) -> None:
    job = Job.load("jobs/read_savings.yaml")
    inputs = {"operator_id": "teller7", "pin": "1937", "member_id": "12345"}
    discover = await launch_browser(headless=True)
    try:
        result = await DiscoveryLoop(
            discover, ScriptedModel("read"), policy, max_steps=job.max_steps
        ).run(
            goal=job.goal,
            target=job.target,
            inputs=inputs,
            input_names=list(job.inputs),
            required_outputs=list(job.outputs),
            hints=job.discovery_hints,
            preferred_frame=job.preferred_frame,
            click_recoveries=job.click_recoveries,
        )
        capability = compile_recording(result.recording, job, trace_ref="test")
    finally:
        await discover.close()

    for member_id, fault, expected in [
        ("00000", None, "outcome"),
        ("12345", "interstitial", "success"),
    ]:
        surface = await launch_browser(headless=True)
        run_inputs = {**inputs, "member_id": member_id}
        try:
            if fault:
                await surface.act("navigate", value=job.target)
                await surface.page.evaluate(
                    "(value) => sessionStorage.setItem('night-window:fault', value)",
                    fault,
                )
            evidence = EvidenceWriter(
                kind="test-replay",
                goal=job.goal,
                target=job.target,
                model=None,
                redactor=Redactor(list(run_inputs.values())),
                root=tmp_path,
            )
            replay = await ReplayEngine(
                surface=surface, policy=policy, evidence=evidence, allow_draft=True
            ).run(capability, run_inputs)
            assert replay.status == expected
            if member_id == "00000":
                assert replay.code == "MEMBER_NOT_FOUND"
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_mutating_flow_uses_ui_success_and_storage_oracle(
    tmp_path: Path, policy: PolicyEngine, test_bank_server
) -> None:
    job = Job.load("jobs/open_sub_account.yaml")
    inputs = {
        "operator_id": "teller7",
        "pin": "1937",
        "member_id": "12345",
        "product": "Holiday Savings",
        "nickname": "Trip",
        "opening_deposit": "10.00",
    }
    discovery = await launch_browser(headless=True)
    try:
        result = await DiscoveryLoop(
            discovery,
            ScriptedModel("open"),
            policy,
            max_steps=job.max_steps,
            confirmed_risks={Risk.MUTATING},
        ).run(
            goal=job.goal,
            target=job.target,
            inputs=inputs,
            input_names=list(job.inputs),
            required_outputs=list(job.outputs),
            hints=job.discovery_hints,
            preferred_frame=job.preferred_frame,
            click_recoveries=job.click_recoveries,
        )
        capability = compile_recording(result.recording, job, trace_ref="test")
        assert any(step.risk == Risk.MUTATING for step in capability.execution.steps)
    finally:
        await discovery.close()

    surface = await launch_browser(headless=True)
    try:
        await surface.act("navigate", value=job.target)
        await surface.observe()
        before = await surface.storage_snapshot()
        evidence = EvidenceWriter(
            kind="test-replay",
            goal=job.goal,
            target=job.target,
            model=None,
            redactor=Redactor(list(inputs.values())),
            root=tmp_path,
        )
        replay = await ReplayEngine(
            surface=surface,
            policy=policy,
            evidence=evidence,
            allow_mutating=True,
            allow_draft=True,
        ).run(capability, inputs)
        after = await surface.storage_snapshot()
        before_count = len(
            json.loads(before["night-window:ledger"])["members"]["12345"]["accounts"]
        )
        after_count = len(
            json.loads(after["night-window:ledger"])["members"]["12345"]["accounts"]
        )
        assert replay.status == "success"
        assert after_count == before_count + 1
    finally:
        await surface.close()


@pytest.mark.asyncio
async def test_same_session_handoff_and_checkpoint_resume(
    tmp_path: Path, policy: PolicyEngine, test_bank_server
) -> None:
    capability, inputs = await _compile_read_savings(policy)
    surface = await launch_browser(headless=True)
    evidence = EvidenceWriter(
        kind="test-handoff",
        goal=capability.contract.description,
        target=capability.compatibility.surface.entry_point,
        model=None,
        redactor=Redactor(list(inputs.values())),
        root=tmp_path,
    )
    coordinator = HandoffCoordinator(surface, event_sink=evidence.event)
    engine = ReplayEngine(
        surface=surface,
        policy=policy,
        evidence=evidence,
        handoff=coordinator,
        allow_draft=True,
    )
    try:
        await surface.act(
            "navigate", value=capability.compatibility.surface.entry_point
        )
        await surface.page.evaluate(
            "() => sessionStorage.setItem('night-window:fault', 'session_drop')"
        )
        first = await engine.run(capability, inputs)
        assert first.status == "escalated"
        assert first.reason.code == "SESSION_EXPIRED"
        assert (await coordinator.lease.state()).holder is None

        await coordinator.claim(first.intervention_id, "operator-test")
        work = surface.page.frame(name="night-work")
        assert work is not None
        await work.locator("input").nth(0).fill("teller7")
        await work.locator("input").nth(1).fill("1937")
        await work.locator("button").click()
        await work.wait_for_url("**/search.html")
        await work.locator("input").first.fill("12345")
        await work.get_by_text("Search members", exact=True).click()
        await work.get_by_text("MEMBER PROFILE READY").wait_for()
        await coordinator.hand_back(
            first.intervention_id, "operator-test", "Restored the member detail view"
        )

        final = await engine.continue_after_handoff(capability, inputs)
        assert final.status == "success"
        assert final.llm_calls == 0
        audit = (evidence.directory / "handoff.json").read_text(encoding="utf-8")
        assert "operator-test" in audit
        assert '"event": "click"' in audit
        assert "1937" not in audit
        assert "12345" not in audit
        trace = (evidence.directory / "trace.jsonl").read_text(encoding="utf-8")
        assert "handoff_claimed" in trace
        assert "handoff_returned" in trace
        assert "resumed" in trace
        assert '"basis": "live checkpoints"' in trace
    finally:
        await surface.close()


def test_tenant_profile_patches_semantic_labels() -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    specialized = apply_tenant(capability, find_profile("northbay"))
    assert specialized.compatibility.tenant == "northbay"
    assert specialized.compatibility.surface.entry_point.endswith("/tenant-b/")
    assert specialized.compatibility.surface.vendor == "north-bay-credit-union"
    assert "tenant-b" in (specialized.compatibility.fingerprint or "")
    s1 = specialized.execution.steps[0].target.strategies[0]
    assert s1.kind == "semantic"
    assert s1.adjacent_text == "Staff ID"
    assert specialized.execution.steps[2].checkpoint.expected == "Member search"


@pytest.mark.asyncio
async def test_draft_replay_is_rejected_without_override(
    tmp_path: Path, policy: PolicyEngine
) -> None:
    capability = load_capability("capabilities/member.read_savings_balance.json")
    assert capability.governance.approval.status == "draft"
    surface = await launch_browser(headless=True)
    try:
        evidence = EvidenceWriter(
            kind="test-draft",
            goal=capability.contract.description,
            target=capability.compatibility.surface.entry_point,
            model=None,
            redactor=Redactor([]),
            root=tmp_path,
        )
        result = await ReplayEngine(
            surface=surface, policy=policy, evidence=evidence
        ).run(
            capability,
            {"operator_id": "teller7", "pin": "1937", "member_id": "12345"},
        )
        assert result.status == "failure"
        assert result.error.code == "APPROVAL_REQUIRED"
    finally:
        await surface.close()


def test_catalog_lists_and_emits_tool_defs() -> None:
    from pigeonhole.catalog import find_capability, iter_capabilities, tool_definitions

    rows = iter_capabilities()
    ids = {capability.contract.id for _, capability in rows}
    assert "member.read_savings_balance" in ids
    assert find_capability("member.read_savings_balance").name.endswith(".json")
    tools = tool_definitions()
    names = {item["function"]["name"] for item in tools}
    assert "member_read_savings_balance" in names


@pytest.mark.asyncio
async def test_northbay_override_replays_and_bare_tenant_drifts(
    tmp_path: Path, policy: PolicyEngine, test_bank_server
) -> None:
    capability, inputs = await _compile_read_savings(policy)
    profile = find_profile("northbay")
    specialized = apply_tenant(capability, profile)

    surface = await launch_browser(headless=True)
    try:
        evidence = EvidenceWriter(
            kind="test-tenant-b",
            goal=capability.contract.description,
            target=profile.entry_point,
            model=None,
            redactor=Redactor(list(inputs.values()) + ["$1842.37"]),
            root=tmp_path,
        )
        replay = await ReplayEngine(
            surface=surface,
            policy=policy,
            evidence=evidence,
            allow_draft=True,
        ).run(specialized, inputs)
        assert replay.status == "success"
        assert replay.outputs["savings_balance"] == "$1842.37"
        assert replay.override_score > 0
        assert replay.drift_score == 0
        assert any(vote.overridden for vote in replay.locator_votes)
        assert not any(vote.drifted for vote in replay.locator_votes)
    finally:
        await surface.close()

    drifted = capability.model_copy(deep=True)
    drifted.compatibility.surface.entry_point = profile.entry_point
    surface = await launch_browser(headless=True)
    try:
        evidence = EvidenceWriter(
            kind="test-tenant-drift",
            goal=capability.contract.description,
            target=profile.entry_point,
            model=None,
            redactor=Redactor(list(inputs.values())),
            root=tmp_path,
        )
        replay = await ReplayEngine(
            surface=surface,
            policy=policy,
            evidence=evidence,
            allow_draft=True,
        ).run(drifted, inputs)
        assert replay.status in {"failure", "escalated"}
    finally:
        await surface.close()
