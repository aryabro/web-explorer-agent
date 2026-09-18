from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pigeonhole.redact import Redactor

LOCATOR_IDENTITY_KEYS = {"target", "strategies"}


class EvidenceWriter:
    def __init__(
        self,
        *,
        kind: str,
        goal: str,
        target: str,
        model: str | None,
        redactor: Redactor,
        root: str | Path = "evidence",
        run_id: str | None = None,
    ) -> None:
        self.run_id = (
            run_id
            or f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        )
        self.directory = Path(root) / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._trace = self.directory / "trace.jsonl"
        if self._trace.exists():
            self._trace.unlink()
        self.write_json(
            "manifest.json",
            {
                "run_id": self.run_id,
                "kind": kind,
                "goal": goal,
                "target": target,
                "model": model,
                "started_at": datetime.now(UTC).isoformat(),
                "schema_version": "1.0.0",
            },
        )

    async def event(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "at": datetime.now(UTC).isoformat(),
            "type": event_type,
            "payload": self._redact_sink(payload),
        }
        with self._trace.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_json(self, name: str, value: Any) -> Path:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json", exclude_none=True)
        destination = self.directory / name
        destination.write_text(
            json.dumps(self._redact_sink(value), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return destination

    def write_jsonl(self, name: str, rows: list[Any]) -> Path:
        destination = self.directory / name
        with destination.open("w", encoding="utf-8") as handle:
            for row in rows:
                if isinstance(row, BaseModel):
                    row = row.model_dump(mode="json", exclude_none=True)
                handle.write(
                    json.dumps(self._redact_sink(row), ensure_ascii=False) + "\n"
                )
        return destination

    def _redact_sink(self, value: Any) -> Any:
        return _redact_preserving_locators(self.redactor, value)


def _redact_preserving_locators(redactor: Redactor, value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                item
                if key in LOCATOR_IDENTITY_KEYS
                else _redact_preserving_locators(redactor, item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_preserving_locators(redactor, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_preserving_locators(redactor, item) for item in value)
    if isinstance(value, str):
        return redactor.text(value)
    return value
