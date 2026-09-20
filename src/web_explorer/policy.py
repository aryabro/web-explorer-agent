from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel

from web_explorer.contracts import Risk
from web_explorer.surface.base import ControlObservation


class PolicyDecision(BaseModel):
    disposition: Literal["allow", "deny", "confirm"]
    reason: str


class PolicyEngine:
    def __init__(self, document: dict) -> None:
        self.document = document

    @classmethod
    def load(cls, path: str | Path = "policy.yaml") -> "PolicyEngine":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def location(*, action: str, current_url: str, value: Any = None) -> str:
        """Policy is evaluated against the destination of a navigation."""
        if action == "navigate" and value:
            return str(value)
        return current_url

    def evaluate(
        self,
        *,
        url: str,
        action: str,
        risk: Risk | str,
        intent: str = "",
    ) -> PolicyDecision:
        location = self.evaluate_location(url)
        if location.disposition == "deny":
            return location
        if action not in self.document["allowed_actions"]:
            return PolicyDecision(
                disposition="deny", reason=f"action is not allowlisted: {action}"
            )
        for pattern in self.document.get("deny_text_patterns", []):
            if re.search(pattern, intent):
                return PolicyDecision(
                    disposition="deny", reason="intent matched a deny pattern"
                )
        disposition = self.document["risk"].get(str(risk), "deny")
        return PolicyDecision(
            disposition=disposition,
            reason=f"{risk} actions are configured as {disposition}",
        )

    def evaluate_location(self, url: str) -> PolicyDecision:
        """Check only whether a currently observed location is allowlisted."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.document["allowed_origins"]:
            return PolicyDecision(
                disposition="deny", reason=f"origin is not allowlisted: {origin}"
            )
        if not self._path_allowed(parsed.path):
            return PolicyDecision(
                disposition="deny", reason=f"path is not allowlisted: {parsed.path}"
            )
        return PolicyDecision(
            disposition="allow", reason="location is allowlisted"
        )

    def infer_risk(
        self,
        *,
        action: str,
        control: ControlObservation | None = None,
    ) -> Risk:
        """Trusted risk class. Model-declared risk is advisory only."""
        haystack = _control_label(control)
        inference = self.document.get("risk_inference") or {}
        for pattern in inference.get("irreversible", []):
            if haystack and re.search(pattern, haystack):
                return Risk.IRREVERSIBLE
        if action in {"click", "select", "type"}:
            for pattern in inference.get("mutating", []):
                if haystack and re.search(pattern, haystack):
                    return Risk.MUTATING
        return Risk.SAFE

    def _path_allowed(self, path: str) -> bool:
        normalized = path or "/"
        for prefix in self.document["allowed_path_prefixes"]:
            if prefix == "/":
                if normalized == "/":
                    return True
                continue
            if normalized.startswith(prefix):
                return True
        return False


def _control_label(control: ControlObservation | None) -> str:
    if control is None:
        return ""
    return " ".join(
        part for part in (control.visible_text, control.accessible_name) if part
    )
