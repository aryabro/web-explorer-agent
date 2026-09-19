from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pigeonhole.compiler import Job
from pigeonhole.contracts import Recovery
from pigeonhole.surface.base import SurfaceResolutionError
from pigeonhole.surface.resolution import vote_identities


def test_replay_module_has_no_llm_or_discovery_dependency() -> None:
    source = Path("src/pigeonhole/replay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "pigeonhole.discovery",
        "pigeonhole.scripted_model",
        "openai",
        "httpx",
        "anthropic",
        "google.generativeai",
    }
    assert imported.isdisjoint(forbidden)
    assert "OpenAICompatibleModel" not in source
    assert "LLMConfig" not in source
    assert "llm_calls=" not in source
    contracts = Path("src/pigeonhole/contracts.py").read_text(encoding="utf-8")
    assert "llm_calls: Literal[0]" in contracts
    from pigeonhole.contracts import SuccessResult

    assert SuccessResult(outputs={}, completed_steps=[]).llm_calls == 0


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
