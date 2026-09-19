from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> "LLMConfig":
        key = os.getenv("CUA_LLM_API_KEY", "")
        if not key:
            raise RuntimeError("CUA_LLM_API_KEY is required for genuine discovery")
        return cls(
            base_url=os.getenv("CUA_LLM_BASE_URL", "https://api.openai.com/v1").rstrip(
                "/"
            ),
            api_key=key,
            model=os.getenv("CUA_LLM_MODEL", "gpt-4.1-mini"),
        )


def target_url() -> str:
    return os.getenv("NIGHT_WINDOW_URL", "http://127.0.0.1:8765")


def runtime_pin() -> str:
    return os.getenv("NIGHT_WINDOW_PIN", "1937")
