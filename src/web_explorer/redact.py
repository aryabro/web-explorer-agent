from __future__ import annotations

import re
from typing import Any


class Redactor:
    def __init__(self, known_values: list[str] | None = None) -> None:
        self.known_values = sorted(
            {str(value) for value in (known_values or []) if str(value)},
            key=len,
            reverse=True,
        )
        self.patterns = [
            re.compile(
                r"(?i)(Member name\s+).+?(?=\s+(?:Member|Profile|Jacket) status)"
            ),
            re.compile(
                r"(?i)(Member profile\s+)[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+)+"
                r"(?=\s+Member\b)"
            ),
            re.compile(r"\b(?:S|C)-\d{4}\s+(?:Savings|Checking)\s+.+?(?=\s+\$)"),
            re.compile(r"\b[A-Z]-\d{4,}\b"),
            # A digit run directly after a decimal point is a fraction, not an
            # identifier; without this, timestamp microseconds are redacted.
            re.compile(r"(?<!\.)\b\d{5,12}\b"),
            re.compile(r"\$\s?\d[\d,]*\.\d{2}"),
            re.compile(r"(?i)(pin|password|token)\s*[:=]\s*\S+"),
        ]

    def text(self, value: str) -> str:
        result = value
        for known in self.known_values:
            result = result.replace(known, "[REDACTED]")
        for pattern in self.patterns:
            result = pattern.sub("[REDACTED]", result)
        return result

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {key: self.data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.data(item) for item in value)
        return value

    def for_model(self, value: Any) -> Any:
        """Redact a prompt payload before it leaves the process toward an LLM."""
        return self.data(value)
