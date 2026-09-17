from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from pigeonhole.surface.base import SurfaceDriver


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
    raised_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["waiting", "operator", "returned", "aborted"] = "waiting"
    operator_id: str | None = None
    note: str | None = None


class HandoffCoordinator:
    def __init__(self, surface: SurfaceDriver, lease: SessionLease | None = None) -> None:
        self.surface = surface
        self.lease = lease or SessionLease()
        self.interventions: dict[str, Intervention] = {}
        self._returned: dict[str, asyncio.Event] = {}
        self._directories: dict[str, Path] = {}

    async def raise_intervention(
        self,
        *,
        capability_id: str,
        goal: str,
        diagnostic: Any,
        evidence_directory: Path,
        redact_values: list[str] | None = None,
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
        )
        self.interventions[intervention.id] = intervention
        self._returned[intervention.id] = asyncio.Event()
        self._directories[intervention.id] = evidence_directory
        evidence_directory.mkdir(parents=True, exist_ok=True)
        (evidence_directory / "intervention.json").write_text(
            intervention.model_dump_json(indent=2), encoding="utf-8"
        )
        await self.surface.screenshot(
            str(evidence_directory / "screenshots" / "intervention.png"),
            redact_values,
        )
        return intervention

    async def claim(self, intervention_id: str, operator_id: str) -> Intervention:
        intervention = self._get(intervention_id)
        await self.lease.claim(operator_id)
        intervention.status = "operator"
        intervention.operator_id = operator_id
        self._persist(intervention)
        return intervention

    async def hand_back(
        self, intervention_id: str, operator_id: str, note: str = ""
    ) -> Intervention:
        intervention = self._get(intervention_id)
        await self.lease.hand_back(operator_id)
        events = await self.surface.stop_human_capture()
        intervention.status = "returned"
        intervention.note = note
        self._persist(intervention)
        lease_state = await self.lease.state()
        directory = self._directories[intervention_id]
        (directory / "handoff.json").write_text(
            json.dumps(
                {
                    "holder": lease_state.holder,
                    "operator_id": operator_id,
                    "capability_id": intervention.capability_id,
                    "goal": intervention.goal,
                    "reason": intervention.diagnostic,
                    "human_events": events,
                    "intervention_id": intervention_id,
                    "returned_at": datetime.now(UTC).isoformat(),
                    "note": note,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        await self.surface.resume()
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
        (self._directories[intervention.id] / "intervention.json").write_text(
            intervention.model_dump_json(indent=2), encoding="utf-8"
        )

    def operator_app(self) -> FastAPI:
        app = FastAPI(title="Pigeonhole operator handoff", docs_url=None)
        coordinator = self

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            cards = "".join(
                f"""<li><b>{item.capability_id}</b> — {item.status}
                <pre>{json.dumps(item.diagnostic, indent=2)}</pre>
                <button onclick="claim('{item.id}')">Claim</button>
                <button onclick="giveBack('{item.id}')">Hand back</button></li>"""
                for item in coordinator.interventions.values()
            ) or "<li>No intervention is waiting.</li>"
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
<html><head><title>Pigeonhole operator</title>
<style>body{font-family:system-ui;max-width:760px;margin:2rem auto}li{margin:1rem;padding:1rem;border:1px solid #777}pre{white-space:pre-wrap}</style>
</head><body><h1>Live-session handoff</h1>
<p>Claim the lease here, operate the already-open headed Chromium window directly,
then hand it back. This console does not proxy browser input.</p>
<ul>{{CARDS}}</ul>
<script>
const operator_id = 'local-operator';
async function post(url, body) {
  const result = await fetch(url, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
  if (!result.ok) alert(await result.text()); else location.reload();
}
function claim(id){post(`/interventions/${id}/claim`, {operator_id});}
function giveBack(id){post(`/interventions/${id}/hand-back`, {operator_id, note:prompt('What did you do?') || ''});}
</script></body></html>"""

