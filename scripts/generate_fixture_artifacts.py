"""Generate deterministic artifacts/evidence for local tests and demos.

The resulting discovery directories are explicitly marked as fixture evidence.
They do not satisfy the assignment's genuine LLM discovery requirement.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pigeonhole.compiler import Job, compile_recording, save_capability
from pigeonhole.contracts import Risk
from pigeonhole.discovery import DiscoveryLoop
from pigeonhole.evidence import EvidenceWriter
from pigeonhole.fixture_model import ScriptedModel
from pigeonhole.policy import PolicyEngine
from pigeonhole.redact import Redactor
from pigeonhole.replay import ReplayEngine
from pigeonhole.tenants import apply_tenant, find_profile
from target.profile import launch_browser

FIXTURE_ROOT = Path("evidence/fixture")
POLICY = PolicyEngine.load()


async def discover(
    flow: str, job_path: str, inputs: dict[str, str], *, save_to: Path | None = None
):
    job = Job.load(job_path)
    redactor = Redactor(list(inputs.values()))
    writer = EvidenceWriter(
        kind="fixture-discovery",
        goal=job.goal,
        target=job.target,
        model=ScriptedModel.name,
        redactor=redactor,
        root=FIXTURE_ROOT,
        run_id=f"{flow}-discovery",
    )
    surface = await launch_browser(headless=True)
    try:
        result = await DiscoveryLoop(
            surface,
            ScriptedModel(flow),
            POLICY,
            max_steps=job.max_steps,
            event_sink=writer.event,
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
        if not result.recording.success:
            raise RuntimeError(result.recording.stop_reason)
        writer.write_jsonl("recording.jsonl", result.recording.steps)
        writer.write_json("result.json", result)
        capability = compile_recording(
            result.recording,
            job,
            trace_ref=f"evidence/fixture/{flow}-discovery/trace.jsonl",
        )
        if save_to is not None:
            save_capability(capability, save_to)
        return job, capability
    finally:
        await surface.close()


async def replay(
    name: str,
    job: Job,
    capability,
    inputs: dict[str, str],
    *,
    fault: str | None = None,
    allow_mutating: bool = False,
    storage_oracle: bool = False,
) -> None:
    writer = EvidenceWriter(
        kind="fixture-replay",
        goal=job.goal,
        target=job.target,
        model=None,
        redactor=Redactor(list(inputs.values())),
        root=FIXTURE_ROOT,
        run_id=name,
    )
    surface = await launch_browser(headless=True)
    try:
        await surface.act("navigate", value=job.target)
        await surface.observe()
        before = await surface.storage_snapshot()
        if fault:
            await surface.page.evaluate(
                "(value) => sessionStorage.setItem('night-window:fault', value)",
                fault,
            )
        result = await ReplayEngine(
            surface=surface,
            policy=POLICY,
            evidence=writer,
            allow_mutating=allow_mutating,
            allow_draft=True,
        ).run(capability, inputs)
        if storage_oracle:
            after = await surface.storage_snapshot()
            before_ledger = json.loads(before["night-window:ledger"])
            after_ledger = json.loads(after["night-window:ledger"])
            member = inputs["member_id"]
            writer.write_json(
                "storage-oracle.json",
                {
                    "member_id": member,
                    "before_account_count": len(
                        before_ledger["members"][member]["accounts"]
                    ),
                    "after_account_count": len(
                        after_ledger["members"][member]["accounts"]
                    ),
                    "assertion": "after_account_count == before_account_count + 1",
                    "passed": len(after_ledger["members"][member]["accounts"])
                    == len(before_ledger["members"][member]["accounts"]) + 1,
                },
            )
        print(name, result.status)
    finally:
        await surface.close()


async def main() -> None:
    read_inputs = {"operator_id": "teller7", "pin": "1937", "member_id": "12345"}
    read_job, read_capability = await discover(
        "read",
        "jobs/read_savings.yaml",
        read_inputs,
        save_to=FIXTURE_ROOT / "capabilities" / "member.read_savings_balance.json",
    )
    await replay("read-replay-success", read_job, read_capability, read_inputs)
    await replay(
        "read-replay-not-found",
        read_job,
        read_capability,
        {**read_inputs, "member_id": "00000"},
    )
    await replay(
        "read-replay-interstitial",
        read_job,
        read_capability,
        read_inputs,
        fault="interstitial",
    )
    await replay(
        "read-replay-session-expired",
        read_job,
        read_capability,
        read_inputs,
        fault="session_drop",
    )
    northbay = apply_tenant(read_capability, find_profile("northbay"))
    await replay("read-replay-northbay", read_job, northbay, read_inputs)
    drifted = read_capability.model_copy(deep=True)
    drifted.compatibility.surface.entry_point = northbay.compatibility.surface.entry_point
    await replay("read-replay-northbay-drift", read_job, drifted, read_inputs)

    open_inputs = {
        **read_inputs,
        "product": "Holiday Savings",
        "nickname": "Trip",
        "opening_deposit": "10.00",
    }
    open_job, open_capability = await discover(
        "open",
        "jobs/open_sub_account.yaml",
        open_inputs,
        save_to=Path("capabilities") / "member.open_sub_account.json",
    )
    await replay(
        "open-replay-success",
        open_job,
        open_capability,
        open_inputs,
        allow_mutating=True,
        storage_oracle=True,
    )


if __name__ == "__main__":
    asyncio.run(main())

