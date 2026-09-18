"""Write replay evidence bundles against the committed genuine capability.

Does not overwrite the committed genuine discovery bundle.
Requires nothing except Chromium; starts Test Bank Operations on :8765 if needed.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path

import uvicorn

from pigeonhole.catalog import iter_capabilities, summarize, tool_definitions
from pigeonhole.contracts import Risk
from pigeonhole.evidence import EvidenceWriter
from pigeonhole.handoff import HandoffCoordinator
from pigeonhole.policy import PolicyEngine
from pigeonhole.redact import Redactor
from pigeonhole.replay import ReplayEngine, load_capability
from pigeonhole.tenants import apply_tenant, find_profile
from target.profile import launch_browser
from target.server import app as night_window

ROOT = Path("evidence")
POLICY = PolicyEngine.load()
CAPABILITY = load_capability("capabilities/member.read_savings_balance.json")
INPUTS = {"operator_id": "teller7", "pin": "1937", "member_id": "12345"}
GENUINE_DISCOVERY = "discovery-20260918T020425Z-0f25df"


def _ensure_night_window() -> None:
    with socket.socket() as client:
        if client.connect_ex(("127.0.0.1", 8765)) == 0:
            return
    config = uvicorn.Config(night_window, host="127.0.0.1", port=8765, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        with socket.socket() as client:
            if client.connect_ex(("127.0.0.1", 8765)) == 0:
                return
        threading.Event().wait(0.05)
    raise RuntimeError("Test Bank Operations did not start")


async def _engine(surface, writer, *, allow_draft=True, allow_mutating=False, handoff=None):
    return ReplayEngine(
        surface=surface,
        policy=POLICY,
        evidence=writer,
        handoff=handoff,
        allow_draft=allow_draft,
        allow_mutating=allow_mutating,
    )


def _needs_mutating(capability) -> bool:
    return any(step.risk == Risk.MUTATING for step in capability.execution.steps)


def _writer(run_id: str, inputs: dict[str, str], extra: list[str] | None = None):
    redactor = Redactor(list(inputs.values()) + (extra or ["$1842.37"]))
    return EvidenceWriter(
        kind="replay",
        goal=CAPABILITY.contract.description,
        target=CAPABILITY.compatibility.surface.entry_point,
        model=None,
        redactor=redactor,
        root=ROOT,
        run_id=run_id,
    )


async def replay_named(
    run_id: str,
    *,
    capability=None,
    inputs: dict[str, str] | None = None,
    allow_draft: bool = True,
    fault: str | None = None,
    handoff: bool = False,
) -> None:
    capability = capability or CAPABILITY
    inputs = inputs or INPUTS
    writer = _writer(run_id, inputs)
    surface = await launch_browser(headless=True)
    coordinator = HandoffCoordinator(surface) if handoff else None
    try:
        engine = await _engine(
            surface,
            writer,
            allow_draft=allow_draft,
            allow_mutating=_needs_mutating(capability),
            handoff=coordinator,
        )
        if fault:
            blocked = await engine.open_entry(capability, inputs)
            if blocked is not None:
                print(run_id, blocked.status, getattr(blocked, "error", None))
                return
            await surface.page.evaluate(
                "(value) => sessionStorage.setItem('night-window:fault', value)",
                fault,
            )
        result = await engine.run(capability, inputs, navigate=not fault)
        print(run_id, result.status, getattr(result, "error", None) or getattr(result, "code", None))
    finally:
        await surface.close()


async def replay_handoff(run_id: str = "replay-handoff") -> None:
    writer = _writer(run_id, INPUTS)
    surface = await launch_browser(headless=True)
    coordinator = HandoffCoordinator(surface, event_sink=writer.event)
    engine = await _engine(
        surface,
        writer,
        handoff=coordinator,
        allow_mutating=_needs_mutating(CAPABILITY),
    )
    try:
        blocked = await engine.open_entry(CAPABILITY, INPUTS)
        assert blocked is None
        await surface.page.evaluate(
            "() => sessionStorage.setItem('night-window:fault', 'session_drop')"
        )
        first = await engine.run(CAPABILITY, INPUTS, navigate=False)
        assert first.status == "escalated"
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
        final = await engine.continue_after_handoff(CAPABILITY, INPUTS)
        print(run_id, first.status, final.status)
    finally:
        await surface.close()


async def main() -> None:
    if not (ROOT / GENUINE_DISCOVERY).exists():
        raise SystemExit(f"keep {GENUINE_DISCOVERY}; it is missing")
    _ensure_night_window()
    ROOT.mkdir(exist_ok=True)
    (ROOT / "catalog.json").write_text(
        json.dumps(
            {
                "capabilities": [
                    summarize(capability, path)
                    for path, capability in iter_capabilities()
                ],
                "tools": tool_definitions(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote evidence/catalog.json")
    await replay_named("replay-happy-12345")
    await replay_named(
        "replay-member-not-found",
        inputs={**INPUTS, "member_id": "00000"},
    )
    await replay_named("replay-interstitial", fault="interstitial")
    await replay_named("replay-session-expired", fault="session_drop", handoff=False)
    await replay_named("replay-draft-denied", allow_draft=False)
    await replay_handoff()
    northbay = apply_tenant(CAPABILITY, find_profile("northbay"))
    await replay_named("replay-northbay", capability=northbay)
    drifted = CAPABILITY.model_copy(deep=True)
    drifted.compatibility.surface.entry_point = (
        northbay.compatibility.surface.entry_point
    )
    await replay_named("replay-northbay-drift", capability=drifted)


if __name__ == "__main__":
    asyncio.run(main())
