import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("DEVFLOW_MODEL", "claude-opus-5-5")
    # Opus 5.5 defaults to "medium" effort; set it explicitly so behaviour is predictable.
    effort: str = os.getenv("DEVFLOW_EFFORT", "medium")
    max_tokens: int = int(os.getenv("DEVFLOW_MAX_TOKENS", "16000"))


settings = Settings()
