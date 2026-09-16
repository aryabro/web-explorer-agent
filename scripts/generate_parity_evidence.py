"""Write locator-vote, tenant, draft-gate, and catalog evidence.

Uses the committed genuine read-savings capability. Does not overwrite it.
Requires Night Window on :8765.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pigeonhole.catalog import iter_capabilities, summarize, tool_definitions
from pigeonhole.evidence import EvidenceWriter
from pigeonhole.policy import PolicyEngine
from pigeonhole.redact import Redactor
from pigeonhole.replay import ReplayEngine, load_capability
from pigeonhole.surface.playwright import PlaywrightSurface
from pigeonhole.tenants import apply_tenant, find_profile

ROOT = Path("evidence")
POLICY = PolicyEngine.load()
CAPABILITY = load_capability("capabilities/member.read_savings_balance.json")
INPUTS = {"operator_id": "teller7", "pin": "1937", "member_id": "12345"}


async def replay(
    run_id: str,
    capability,
    inputs: dict[str, str],
    *,
    allow_draft: bool = True,
) -> None:
    redactor = Redactor(list(inputs.values()) + ["$1842.37"])
    writer = EvidenceWriter(
        kind="replay",
        goal=capability.contract.description,
        target=capability.compatibility.surface.entry_point,
        model=None,
        redactor=redactor,
        root=ROOT,
        run_id=run_id,
    )
    surface = await PlaywrightSurface.launch(headless=True)
    try:
        result = await ReplayEngine(
            surface=surface,
            policy=POLICY,
            evidence=writer,
            allow_draft=allow_draft,
        ).run(capability, inputs)
        print(run_id, result.status, getattr(result, "drift_score", None))
    finally:
        await surface.close()


async def main() -> None:
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
    await replay("replay-locator-votes", CAPABILITY, INPUTS)
    northbay = apply_tenant(CAPABILITY, find_profile("northbay"))
    await replay("replay-northbay", northbay, INPUTS)
    drifted = CAPABILITY.model_copy(deep=True)
    drifted.compatibility.surface.entry_point = northbay.compatibility.surface.entry_point
    await replay("replay-northbay-drift", drifted, INPUTS)
    await replay("replay-draft-denied", CAPABILITY, INPUTS, allow_draft=False)


if __name__ == "__main__":
    asyncio.run(main())
