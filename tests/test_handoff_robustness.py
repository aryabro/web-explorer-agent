from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from web_explorer.handoff import OPERATOR_HTML, HandoffCoordinator, Intervention
from web_explorer.redact import Redactor


class CaptureFailureSurface:
    def __init__(self) -> None:
        self.resumed = False

    async def stop_human_capture(self) -> list[dict[str, Any]]:
        raise RuntimeError("browser execution context was replaced")

    async def resume(self) -> None:
        self.resumed = True


class SensitiveCaptureSurface:
    async def start_human_capture(self) -> None:
        return None

    async def stop_human_capture(self) -> list[dict[str, Any]]:
        return [
            {
                "event": "click",
                "visible_text": "Member 12345 balance $1842.37",
            }
        ]

    async def screenshot(self, path: str, redact_values=None) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def resume(self) -> None:
        return None


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


@pytest.mark.asyncio
async def test_handoff_files_redact_diagnostics_events_and_notes(
    tmp_path: Path,
) -> None:
    secrets = ["12345", "$1842.37", "private operator note"]
    coordinator = HandoffCoordinator(
        SensitiveCaptureSurface(),  # type: ignore[arg-type]
        redactor=Redactor(secrets),
    )
    intervention = await coordinator.raise_intervention(
        capability_id="member.read_savings_balance",
        goal="Read balance for member 12345",
        diagnostic={"message": "Unexpected balance $1842.37 for member 12345"},
        evidence_directory=tmp_path,
        redact_values=secrets,
        step_id="s5",
    )
    await coordinator.claim(intervention.id, "operator-test")
    await coordinator.hand_back(
        intervention.id, "operator-test", "private operator note"
    )

    persisted = "\n".join(
        (tmp_path / name).read_text(encoding="utf-8")
        for name in ("intervention.json", "handoff.json")
    )
    assert "[REDACTED]" in persisted
    assert all(secret not in persisted for secret in secrets)
