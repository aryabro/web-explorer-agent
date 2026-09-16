from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel

from pigeonhole.contracts import Risk


class PolicyDecision(BaseModel):
    disposition: Literal["allow", "deny", "confirm"]
    reason: str


class PolicyEngine:
    def __init__(self, document: dict) -> None:
        self.document = document

    @classmethod
    def load(cls, path: str | Path = "policy.yaml") -> "PolicyEngine":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def evaluate(
        self,
        *,
        url: str,
        action: str,
        risk: Risk | str,
        intent: str = "",
    ) -> PolicyDecision:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.document["allowed_origins"]:
            return PolicyDecision(
                disposition="deny", reason=f"origin is not allowlisted: {origin}"
            )
        if not any(
            parsed.path.startswith(prefix)
            for prefix in self.document["allowed_path_prefixes"]
        ):
            return PolicyDecision(
                disposition="deny", reason=f"path is not allowlisted: {parsed.path}"
            )
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

