import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("DEVFLOW_MODEL", "claude-opus-5-5")
    # Every model runs at medium effort (fixed by design, not configurable). Per-step models live in routing.py.
    effort: str = "medium"
    max_tokens: int = int(os.getenv("DEVFLOW_MAX_TOKENS", "16000"))
    # Where the AI steps run: "api" (Anthropic SDK, needs ANTHROPIC_API_KEY), "claude-cli" (headless Claude Code on
    # your Claude Pro/Max login, no API key) or "auto" (api when ANTHROPIC_API_KEY is set, otherwise claude-cli).
    backend: str = os.getenv("DEVFLOW_LLM_BACKEND", "auto")


def llm_backend(cfg: Settings | None = None, env=os.environ) -> str:
    """Resolves "auto" to the backend the AI steps will actually use."""
    chosen = (cfg or settings).backend.strip().lower()
    if chosen in ("api", "claude-cli"):
        return chosen
    return "api" if env.get("ANTHROPIC_API_KEY") else "claude-cli"


settings = Settings()
