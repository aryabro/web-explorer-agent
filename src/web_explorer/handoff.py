from __future__ import annotations

import asyncio
import html
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from web_explorer.redact import Redactor
from web_explorer.surface.base import SurfaceDriver


class LeaseState(BaseModel):
    holder: Literal["automation", "operator"] | None = "automation"
    operator_id: str | None = None
    expires_at: datetime | None = None


class SessionLease:
    """Fail-closed ownership: an expired lease becomes unowned, never automated."""

    def __init__(self) -> None:
        self._state = LeaseState()
        self._lock = asyncio.Lock()

    async def state(self) -> LeaseState:
        async with self._lock:
            self._expire()
            return self._state.model_copy()

    def _expire(self) -> None:
        if self._state.expires_at and self._state.expires_at <= datetime.now(UTC):
            self._state = LeaseState(holder=None)

    async def assert_automation(self) -> None:
        state = await self.state()
        if state.holder != "automation":
            raise PermissionError("automation does not hold the live-session lease")

    async def cede(self) -> None:
        async with self._lock:
            self._expire()
            if self._state.holder != "automation":
                raise RuntimeError("only automation may cede its lease")
            self._state = LeaseState(holder=None)

    async def claim(self, operator_id: str, *, ttl_seconds: int = 900) -> None:
        async with self._lock:
            self._expire()
            if self._state.holder is not None:
                raise RuntimeError("live session is already owned")
            self._state = LeaseState(
                holder="operator",
                operator_id=operator_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )

    async def hand_back(self, operator_id: str) -> None:
        async with self._lock:
            self._expire()
            if (
                self._state.holder != "operator"
                or self._state.operator_id != operator_id
            ):
                raise RuntimeError("only the owning operator may hand control back")
            self._state = LeaseState(holder="automation")


class Intervention(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    capability_id: str
    goal: str
    diagnostic: dict[str, Any]
    step_id: str | None = None
    raised_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["waiting", "operator", "returned", "aborted"] = "waiting"
    operator_id: str | None = None
    note: str | None = None


async def _noop_event(_: str, __: dict[str, Any]) -> None:
    return None


class HandoffCoordinator:
    def __init__(
        self,
        surface: SurfaceDriver,
        lease: SessionLease | None = None,
        event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.surface = surface
        self.lease = lease or SessionLease()
        self.event = event_sink or _noop_event
        self.redactor = redactor or Redactor()
        self.interventions: dict[str, Intervention] = {}
        self._returned: dict[str, asyncio.Event] = {}
        self._directories: dict[str, Path] = {}
        self._redactors: dict[str, Redactor] = {}

    async def raise_intervention(
        self,
        *,
        capability_id: str,
        goal: str,
        diagnostic: Any,
        evidence_directory: Path,
        redact_values: list[str] | None = None,
        step_id: str | None = None,
    ) -> Intervention:
        await self.lease.cede()
        await self.surface.start_human_capture()
        diagnostic_data = (
            diagnostic.model_dump(mode="json")
            if hasattr(diagnostic, "model_dump")
            else dict(diagnostic)
        )
        intervention = Intervention(
            capability_id=capability_id,
            goal=goal,
            diagnostic=diagnostic_data,
            step_id=step_id or diagnostic_data.get("step_id"),
        )
        self.interventions[intervention.id] = intervention
        self._returned[intervention.id] = asyncio.Event()
        self._directories[intervention.id] = evidence_directory
        self._redactors[intervention.id] = Redactor(
            [*self.redactor.known_values, *(redact_values or [])]
        )
        evidence_directory.mkdir(parents=True, exist_ok=True)
        self._write_json(
            evidence_directory / "intervention.json",
            intervention.model_dump(mode="json", exclude_none=True),
            intervention.id,
        )
        await self.surface.screenshot(
            str(evidence_directory / "screenshots" / "intervention.png"),
            redact_values,
        )
        await self.event(
            "handoff_raised",
            {
                "intervention_id": intervention.id,
                "capability_id": capability_id,
                "step_id": intervention.step_id,
                "holder": None,
            },
        )
        return intervention

    async def claim(self, intervention_id: str, operator_id: str) -> Intervention:
        intervention = self._get(intervention_id)
        await self.lease.claim(operator_id)
        intervention.status = "operator"
        intervention.operator_id = operator_id
        self._persist(intervention)
        await self.event(
            "handoff_claimed",
            {
                "intervention_id": intervention.id,
                "operator_id": operator_id,
                "holder": "operator",
                "step_id": intervention.step_id,
            },
        )
        return intervention

    async def hand_back(
        self, intervention_id: str, operator_id: str, note: str = ""
    ) -> Intervention:
        intervention = self._get(intervention_id)
        await self.lease.hand_back(operator_id)
        # Capture is audit telemetry, not a prerequisite for returning the session.
        # Navigation may invalidate a Playwright handle while capture is stopped;
        # that must not strand a run after its lease returned to automation.
        events: list[dict[str, Any]] = []
        capture_error: str | None = None
        try:
            events = await self.surface.stop_human_capture()
        except Exception as exc:  # noqa: BLE001 - telemetry must not block hand-back
            capture_error = type(exc).__name__
            await self.event(
                "human_capture_failed",
                {
                    "intervention_id": intervention_id,
                    "operator_id": operator_id,
                    "error_type": capture_error,
                    "step_id": intervention.step_id,
                },
            )
        intervention.status = "returned"
        intervention.note = note
        self._persist(intervention)
        lease_state = await self.lease.state()
        directory = self._directories[intervention_id]
        self._write_json(
            directory / "handoff.json",
            {
                "holder": lease_state.holder,
                "operator_id": operator_id,
                "capability_id": intervention.capability_id,
                "goal": intervention.goal,
                "reason": intervention.diagnostic,
                "human_events": events,
                "human_capture_error": capture_error,
                "intervention_id": intervention_id,
                "returned_at": datetime.now(UTC).isoformat(),
                "note": note,
            },
            intervention_id,
        )
        await self.surface.resume()
        await self.event(
            "handoff_returned",
            {
                "intervention_id": intervention_id,
                "operator_id": operator_id,
                "holder": lease_state.holder,
                "human_event_count": len(events),
                "human_capture_error": capture_error,
                "note": note,
                "step_id": intervention.step_id,
            },
        )
        self._returned[intervention_id].set()
        return intervention

    async def wait_for_return(self, intervention_id: str) -> None:
        await self._returned[intervention_id].wait()

    def _get(self, intervention_id: str) -> Intervention:
        try:
            return self.interventions[intervention_id]
        except KeyError as exc:
            raise KeyError("unknown intervention") from exc

    def _persist(self, intervention: Intervention) -> None:
        self._write_json(
            self._directories[intervention.id] / "intervention.json",
            intervention.model_dump(mode="json", exclude_none=True),
            intervention.id,
        )

    def _write_json(self, path: Path, value: Any, intervention_id: str) -> None:
        redactor = self._redactors.get(intervention_id, self.redactor)
        path.write_text(
            json.dumps(redactor.data(value), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def operator_app(self) -> FastAPI:
        app = FastAPI(title="Web Explorer operator handoff", docs_url=None)
        coordinator = self

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            def render_card(item: Intervention) -> str:
                if item.status == "waiting":
                    actions = (
                        f"<button onclick=\"claim('{item.id}')\">Claim control</button>"
                    )
                elif item.status == "operator":
                    actions = (
                        f'<button class="primary" onclick="giveBack(\'{item.id}\')">'
                        "Hand back to automation</button>"
                    )
                else:
                    actions = "<span class=done>No action required.</span>"
                diagnostic = html.escape(json.dumps(item.diagnostic, indent=2))
                return (
                    f"<li><b>{html.escape(item.capability_id)}</b> "
                    f'<span class="badge">{item.status}</span>'
                    f"<pre>{diagnostic}</pre>{actions}</li>"
                )

            cards = (
                "".join(
                    render_card(item) for item in coordinator.interventions.values()
                )
                or "<li>No intervention is waiting.</li>"
            )
            return OPERATOR_HTML.replace("{{CARDS}}", cards)

        @app.post("/interventions/{intervention_id}/claim")
        async def claim_endpoint(intervention_id: str, body: dict[str, str]):
            try:
                return await coordinator.claim(
                    intervention_id, body.get("operator_id", "local-operator")
                )
            except (KeyError, RuntimeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/interventions/{intervention_id}/hand-back")
        async def hand_back_endpoint(intervention_id: str, body: dict[str, str]):
            try:
                return await coordinator.hand_back(
                    intervention_id,
                    body.get("operator_id", "local-operator"),
                    body.get("note", ""),
                )
            except (KeyError, RuntimeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        return app


OPERATOR_HTML = """<!doctype html>
<html><head><title>Web Explorer operator</title>
<style>body{font-family:system-ui;max-width:760px;margin:2rem auto;line-height:1.45}li{margin:1rem 0;padding:1rem;border:1px solid #aaa;border-radius:8px;list-style:none}ul{padding:0}pre{white-space:pre-wrap;background:#f5f5f5;padding:.75rem}.notice{padding:1rem;background:#fff4d6;border-left:4px solid #c57a00}.badge{margin-left:.5rem;padding:.15rem .45rem;background:#eee;border-radius:1rem}.primary{font-weight:700;padding:.5rem .8rem}.done{color:#555}#message{min-height:1.5rem;color:#9b1c1c}</style>
</head><body><h1>Live-session handoff</h1>
<div class="notice"><b>This page is only the control console.</b> After claiming,
use the separate Chromium test-site window to fix the problem. Return here only after
the target page is ready, then click <b>Hand back to automation</b>. Keep the replay
terminal and Chromium window open.</div>
<p>Workflow: 1) claim control; 2) repair the test site in Chromium; 3) return here and hand back.</p>
<div id="message"></div>
<ul>{{CARDS}}</ul>
<script>
const operator_id = 'local-operator';
async function post(url, body) {
  const message = document.getElementById('message');
  message.textContent = 'Working...';
  const result = await fetch(url, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
  if (!result.ok) message.textContent = `Action failed: ${await result.text()}`;
  else location.reload();
}
function claim(id){post(`/interventions/${id}/claim`, {operator_id});}
function giveBack(id){post(`/interventions/${id}/hand-back`, {operator_id, note:''});}
</script></body></html>"""
