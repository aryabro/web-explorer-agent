from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from web_explorer.catalog import (
    find_capability,
    iter_capabilities,
    summarize,
    tool_definitions,
)
from web_explorer.compiler import Job, compile_recording, save_capability
from web_explorer.config import LLMConfig, runtime_pin
from web_explorer.contracts import Approval, Risk
from web_explorer.discovery import DiscoveryLoop, OpenAICompatibleModel
from web_explorer.evidence import EvidenceWriter
from web_explorer.handoff import HandoffCoordinator
from web_explorer.policy import PolicyEngine
from web_explorer.redact import Redactor
from web_explorer.replay import ReplayEngine, load_capability
from web_explorer.tenants import apply_tenant, find_profile
from target.profile import launch_browser

app = typer.Typer(no_args_is_help=True, help="Web Explorer computer-use automation")


def _parse_inputs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(f"input must be NAME=VALUE, got {value!r}")
        name, raw = value.split("=", 1)
        result[name] = raw
    return result


def _runtime_inputs(values: list[str], declared: Iterable[str]) -> dict[str, str]:
    supplied = _parse_inputs(values)
    names = set(declared)
    if "operator_id" in names:
        supplied.setdefault("operator_id", "teller7")
    if "pin" in names:
        supplied.setdefault("pin", runtime_pin())
    return supplied


def _known_sensitive(definitions: dict, values: dict[str, str]) -> list[str]:
    return [
        values[name]
        for name, definition in definitions.items()
        if name in values and definition.sensitivity != "none"
    ]


async def _headed_return(coordinator: HandoffCoordinator, intervention_id: str) -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            coordinator.operator_app(),
            host="127.0.0.1",
            port=8766,
            log_level="warning",
        )
    )
    server_task = asyncio.create_task(server.serve())
    typer.echo(f"intervention {intervention_id}: http://127.0.0.1:8766/")
    try:
        await coordinator.wait_for_return(intervention_id)
    finally:
        server.should_exit = True
        await server_task


@app.command()
def discover(
    job_path: Annotated[Path, typer.Option("--job")] = Path("jobs/read_savings.yaml"),
    input_value: Annotated[list[str], typer.Option("--input")] = [],
    headless: Annotated[bool, typer.Option("--headless/--headed")] = False,
    allow_mutating: Annotated[bool, typer.Option("--allow-mutating")] = False,
) -> None:
    """Run genuine LLM discovery, save recording/evidence, then compile."""
    asyncio.run(_discover(job_path, input_value, headless, allow_mutating))


async def _discover(
    job_path: Path, values: list[str], headless: bool, allow_mutating: bool
) -> None:
    job = Job.load(job_path)
    inputs = _runtime_inputs(values, job.inputs)
    redactor = Redactor(_known_sensitive(job.inputs, inputs))
    model = OpenAICompatibleModel(LLMConfig.from_env())
    evidence = EvidenceWriter(
        kind="discovery",
        goal=job.goal,
        target=job.target,
        model=model.name,
        redactor=redactor,
    )
    surface = await launch_browser(headless=headless)
    coordinator = HandoffCoordinator(surface, event_sink=evidence.event)

    async def on_intervention(intervention) -> bool:
        if headless:
            return False
        await _headed_return(coordinator, intervention.id)
        return True

    try:
        qualification_status: str | None = None
        loop = DiscoveryLoop(
            surface,
            model,
            PolicyEngine.load(),
            max_steps=job.max_steps,
            timeout_seconds=job.discovery_timeout_seconds,
            max_no_progress_steps=job.max_no_progress_steps,
            event_sink=evidence.event,
            confirmed_risks={Risk.MUTATING} if allow_mutating else set(),
            handoff=coordinator,
            evidence=evidence,
            on_intervention=on_intervention,
            capability_id=job.capability_id,
            redact_values=list(inputs.values()),
        )
        result = await loop.run(
            goal=job.goal,
            target=job.target,
            inputs=inputs,
            input_names=list(job.inputs),
            required_outputs=list(job.outputs),
            input_definitions=job.inputs,
            output_definitions=job.outputs,
            business_outcomes=job.business_outcomes,
            fatal_states=job.fatal_states,
            hints=job.discovery_hints,
            preferred_frame=job.preferred_frame,
            click_recoveries=job.click_recoveries,
        )
        evidence.write_jsonl("recording.jsonl", result.recording.steps)
        evidence.write_json("discovery-result.json", result)
        if result.recording.success:
            capability = compile_recording(
                result.recording,
                job,
                trace_ref=(evidence.directory / "trace.jsonl").as_posix(),
            )
            qualification_evidence = EvidenceWriter(
                kind="qualification",
                goal=job.goal,
                target=job.target,
                model=None,
                redactor=redactor,
            )
            qualification_surface = await launch_browser(headless=True)
            try:
                qualification = await ReplayEngine(
                    surface=qualification_surface,
                    policy=PolicyEngine.load(),
                    evidence=qualification_evidence,
                    allow_mutating=allow_mutating,
                    allow_draft=True,
                ).run(capability, inputs)
                qualification_evidence.write_json("result.json", qualification)
                qualification_status = qualification.status
            finally:
                await qualification_surface.close()
            if qualification.status == "success":
                output = Path("capabilities") / f"{job.capability_id}.json"
                save_capability(capability, output)
                typer.echo(f"qualified and compiled {output}")
            else:
                typer.echo(
                    "fresh-session qualification failed; capability was not published"
                )
        typer.echo(
            json.dumps(
                redactor.data(
                    {
                        "status": result.status,
                        "stop_reason": result.recording.stop_reason,
                        "llm_calls": result.llm_calls,
                        "qualification_status": qualification_status,
                        "evidence": str(evidence.directory),
                    }
                ),
                indent=2,
            )
        )
    finally:
        await surface.close()


@app.command()
def replay(
    capability_path: Annotated[Path, typer.Option("--capability")],
    input_value: Annotated[list[str], typer.Option("--input")] = [],
    headless: Annotated[bool, typer.Option("--headless/--headed")] = True,
    allow_mutating: Annotated[bool, typer.Option("--allow-mutating")] = False,
    allow_draft: Annotated[bool, typer.Option("--allow-draft")] = False,
    fault: Annotated[str | None, typer.Option("--fault")] = None,
    handoff: Annotated[bool, typer.Option("--handoff/--no-handoff")] = True,
    tenant: Annotated[str | None, typer.Option("--tenant")] = None,
) -> None:
    """Replay an artifact without invoking an LLM."""
    capability = load_capability(capability_path)
    if tenant:
        capability = apply_tenant(capability, find_profile(tenant))
    asyncio.run(
        _replay(
            capability,
            input_value,
            headless,
            allow_mutating,
            allow_draft,
            fault,
            handoff,
        )
    )


@app.command("call")
def call_capability(
    capability_id: Annotated[str, typer.Option("--id")],
    input_value: Annotated[list[str], typer.Option("--input")] = [],
    headless: Annotated[bool, typer.Option("--headless/--headed")] = True,
    allow_mutating: Annotated[bool, typer.Option("--allow-mutating")] = False,
    allow_draft: Annotated[bool, typer.Option("--allow-draft")] = False,
    fault: Annotated[str | None, typer.Option("--fault")] = None,
    handoff: Annotated[bool, typer.Option("--handoff/--no-handoff")] = True,
    tenant: Annotated[str | None, typer.Option("--tenant")] = None,
) -> None:
    """Invoke a saved capability by id from the local catalog."""
    capability = load_capability(find_capability(capability_id))
    if tenant:
        capability = apply_tenant(capability, find_profile(tenant))
    asyncio.run(
        _replay(
            capability,
            input_value,
            headless,
            allow_mutating,
            allow_draft,
            fault,
            handoff,
        )
    )


async def _replay(
    capability,
    values: list[str],
    headless: bool,
    allow_mutating: bool,
    allow_draft: bool,
    fault: str | None,
    handoff_enabled: bool,
) -> None:
    inputs = _runtime_inputs(values, capability.contract.inputs)
    redactor = Redactor(_known_sensitive(capability.contract.inputs, inputs))
    evidence = EvidenceWriter(
        kind="replay",
        goal=capability.contract.description,
        target=capability.compatibility.surface.entry_point,
        model=None,
        redactor=redactor,
    )
    surface = await launch_browser(headless=headless)
    coordinator = (
        HandoffCoordinator(surface, event_sink=evidence.event)
        if handoff_enabled
        else None
    )
    try:
        engine = ReplayEngine(
            surface=surface,
            policy=PolicyEngine.load(),
            evidence=evidence,
            handoff=coordinator,
            allow_mutating=allow_mutating,
            allow_draft=allow_draft,
        )
        if fault:
            blocked = await engine.open_entry(capability, inputs)
            if blocked is not None:
                typer.echo(json.dumps(blocked.model_dump(mode="json"), indent=2))
                typer.echo(f"evidence: {evidence.directory}")
                return
            await surface.page.evaluate(
                "(fault) => sessionStorage.setItem('night-window:fault', fault)",
                fault,
            )

        async def on_escalated(result) -> None:
            assert coordinator is not None
            await _headed_return(coordinator, result.intervention_id)

        result = await engine.run(
            capability,
            inputs,
            navigate=not fault,
            wait_for_operator=bool(coordinator) and not headless,
            on_escalated=on_escalated if coordinator and not headless else None,
        )
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        typer.echo(f"evidence: {evidence.directory}")
    finally:
        await surface.close()


@app.command()
def approve(
    capability_path: Annotated[Path, typer.Option("--capability")],
    by: Annotated[str, typer.Option("--by")],
) -> None:
    """Mark a compiled capability as approved for unattended replay."""
    capability = load_capability(capability_path)
    capability.governance.approval = Approval(
        status="approved",
        approved_by=by,
        approved_at=datetime.now(UTC),
    )
    save_capability(capability, capability_path)
    typer.echo(f"approved {capability.contract.id} by {by}")


@app.command("list")
def list_capabilities() -> None:
    """Summarize saved capabilities as an agent-facing catalog."""
    rows = [summarize(capability, path) for path, capability in iter_capabilities()]
    typer.echo(json.dumps(rows, indent=2))


@app.command("tools")
def dump_tools() -> None:
    """Emit OpenAI-style tool definitions for saved capabilities."""
    typer.echo(json.dumps(tool_definitions(), indent=2))


@app.command("show-storage")
def show_storage(
    headless: Annotated[bool, typer.Option("--headless/--headed")] = True,
) -> None:
    """Print the independent sessionStorage oracle for a fresh session."""
    asyncio.run(_show_storage(headless))


async def _show_storage(headless: bool) -> None:
    surface = await launch_browser(headless=headless)
    try:
        await surface.act(
            "navigate",
            value=os.getenv("NIGHT_WINDOW_URL", "http://127.0.0.1:8765"),
        )
        typer.echo(json.dumps(await surface.storage_snapshot(), indent=2))
    finally:
        await surface.close()


if __name__ == "__main__":
    app()
