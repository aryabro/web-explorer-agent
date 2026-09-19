from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pigeonhole.handoff import OPERATOR_HTML, HandoffCoordinator, Intervention


class CaptureFailureSurface:
    def __init__(self) -> None:
        self.resumed = False

    async def stop_human_capture(self) -> list[dict[str, Any]]:
        raise RuntimeError("browser execution context was replaced")

    async def resume(self) -> None:
        self.resumed = True


def test_operator_handoff_does_not_prompt_for_a_note() -> None:
    assert "prompt(" not in OPERATOR_HTML
    assert "note:''" in OPERATOR_HTML


@pytest.mark.asyncio
async def test_capture_failure_does_not_strand_operator_handoff(
    tmp_path: Path,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def event_sink(name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    surface = CaptureFailureSurface()
    coordinator = HandoffCoordinator(surface, event_sink=event_sink)  # type: ignore[arg-type]
    intervention = Intervention(
        capability_id="member.read_savings_balance",
        goal="Read a member balance",
        diagnostic={"code": "SESSION_EXPIRED"},
        step_id="s5",
        status="operator",
        operator_id="operator-test",
    )
    coordinator.interventions[intervention.id] = intervention
    coordinator._returned[intervention.id] = asyncio.Event()
    coordinator._directories[intervention.id] = tmp_path
    await coordinator.lease.cede()
    await coordinator.lease.claim("operator-test")

    returned = await coordinator.hand_back(
        intervention.id, "operator-test", "Restored the member profile"
    )

    assert returned.status == "returned"
    assert surface.resumed is True
    assert coordinator._returned[intervention.id].is_set()
    assert (await coordinator.lease.state()).holder == "automation"
    audit = json.loads((tmp_path / "handoff.json").read_text(encoding="utf-8"))
    assert audit["human_events"] == []
    assert audit["human_capture_error"] == "RuntimeError"
    assert [name for name, _ in emitted] == [
        "human_capture_failed",
        "handoff_returned",
    ]
