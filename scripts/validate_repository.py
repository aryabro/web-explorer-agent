"""Validate committed jobs, capabilities, catalog, and evidence without writes."""

from __future__ import annotations

import json
from pathlib import Path

from pigeonhole.catalog import iter_capabilities, summarize, tool_definitions
from pigeonhole.compiler import Job

ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    jobs = sorted((ROOT / "jobs").glob("*.yaml"))
    for path in jobs:
        Job.load(path)

    capabilities = list(iter_capabilities(ROOT / "capabilities"))
    expected_catalog = {
        "capabilities": [
            summarize(capability, path.relative_to(ROOT))
            for path, capability in capabilities
        ],
        "tools": tool_definitions(ROOT / "capabilities"),
    }
    actual_catalog = _load_json(ROOT / "evidence" / "catalog.json")
    if actual_catalog != expected_catalog:
        raise SystemExit(
            "evidence/catalog.json is stale; regenerate it from the capability catalog"
        )

    for _, capability in capabilities:
        trace_ref = capability.governance.provenance.trace_ref
        trace_path = ROOT / trace_ref
        if not trace_path.is_file():
            raise SystemExit(
                f"{capability.contract.id} has a missing provenance trace: {trace_ref}"
            )

    evidence_dirs = 0
    for manifest_path in sorted((ROOT / "evidence").glob("*/manifest.json")):
        manifest = _load_json(manifest_path)
        if manifest.get("run_id") != manifest_path.parent.name:
            raise SystemExit(
                f"{manifest_path}: run_id does not match its directory name"
            )
        evidence_dirs += 1

    json_files = list((ROOT / "evidence").rglob("*.json"))
    for path in json_files:
        _load_json(path)

    jsonl_files = list((ROOT / "evidence").rglob("*.jsonl"))
    for path in jsonl_files:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.strip():
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{path}:{line_number}: {exc}") from exc

    print(
        "repository valid: "
        f"{len(jobs)} jobs, {len(capabilities)} capabilities, "
        f"{evidence_dirs} evidence runs"
    )


if __name__ == "__main__":
    main()
