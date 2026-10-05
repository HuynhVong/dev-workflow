"""Thin wrapper around the Anthropic SDK used by every workflow node.

Nodes only depend on the `StructuredLLM` protocol, so tests can swap in a fake
and you can swap models per workflow without touching graph code.
"""
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Settings, settings as default_settings

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    def structured(self, system: str, prompt: str, schema: type[T]) -> T: ...


class LLMRefusal(RuntimeError):
    pass


class ClaudeLLM:
    def __init__(self, cfg: Settings = default_settings, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        # Created lazily so importing a graph never needs an API key.
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        response = self.client.beta.messages.parse(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": self.cfg.effort},
            # Server-side fallback: if a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise LLMRefusal(f"Claude declined this request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(f"No structured output (stop_reason={response.stop_reason})")
        return response.parsed_output


_default: StructuredLLM | None = None


def default_llm() -> StructuredLLM:
    global _default
    if _default is None:
        _default = ClaudeLLM()
    return _default
