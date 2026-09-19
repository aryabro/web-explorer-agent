from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pigeonhole.compiler import Job
from pigeonhole.contracts import (
    EscalatedResult,
    FailureResult,
    OutcomeResult,
    Recovery,
    SuccessResult,
)
from pigeonhole.surface.base import SurfaceResolutionError
from pigeonhole.surface.resolution import vote_identities

SOURCE_ROOT = Path("src")


def _import_closure(module: str) -> set[str]:
    """Return imports reachable through local pigeonhole modules."""
    pending = [module]
    visited: set[str] = set()
    imported: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        path = SOURCE_ROOT.joinpath(*current.split(".")).with_suffix(".py")
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            imported.update(names)
            pending.extend(name for name in names if name.startswith("pigeonhole."))
    return imported


def test_replay_module_has_no_llm_or_discovery_dependency() -> None:
    imported = _import_closure("pigeonhole.replay")
    forbidden_prefixes = {
        "pigeonhole.config",
        "pigeonhole.discovery",
        "pigeonhole.scripted_model",
        "openai",
        "httpx",
        "anthropic",
        "google.generativeai",
    }
    violations = {
        name
        for name in imported
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in forbidden_prefixes
        )
    }
    assert not violations

    for result_type in (
        SuccessResult,
        OutcomeResult,
        FailureResult,
        EscalatedResult,
    ):
        field = result_type.model_fields["llm_calls"]
        assert field.default == 0
        assert str(field.annotation) == "typing.Literal[0]"


def test_vote_identities_agreement_and_conflict() -> None:
    agreed = vote_identities(
        [("semantic", "el-a"), ("structural", "el-a"), ("geometry", "el-a")]
    )
    assert agreed.agreement == 3
    assert agreed.winners == ["semantic", "structural", "geometry"]
    assert not agreed.weak
    with pytest.raises(SurfaceResolutionError) as conflict:
        vote_identities([("semantic", "el-a"), ("structural", "el-b")])
    assert conflict.value.conflict
    with pytest.raises(SurfaceResolutionError) as unresolved:
        vote_identities([])
    assert not unresolved.value.conflict


def test_recovery_legacy_and_structured_shapes() -> None:
    legacy = Recovery.model_validate(
        {
            "kind": "dismiss_interstitial",
            "visible_text": "NOTICE FROM RECORDS",
            "action_text": "Continue lookup",
        }
    )
    assert legacy.strategy == "dismiss"
    assert legacy.trigger.expected == "NOTICE FROM RECORDS"
    assert legacy.max_attempts == 1
    structured = Recovery.model_validate(
        {
            "trigger": {
                "id": "recover-index",
                "kind": "visible_text",
                "expected": "Index turning...",
            },
            "strategy": "wait",
            "timeout_ms": 3000,
            "max_attempts": 1,
        }
    )
    assert structured.strategy == "wait"
    job = Job.load("jobs/read_savings.yaml")
    assert job.click_recoveries[0].strategy == "dismiss"
    assert job.click_recoveries[1].timeout_ms == 3000
