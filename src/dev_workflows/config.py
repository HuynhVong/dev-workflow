import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("DEVFLOW_MODEL", "claude-opus-5-5")
    # Every model runs at medium effort (fixed by design, not configurable). Per-step models live in routing.py.
    effort: str = "medium"
    max_tokens: int = int(os.getenv("DEVFLOW_MAX_TOKENS", "16000"))


settings = Settings()
