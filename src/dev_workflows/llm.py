"""Thin wrapper around the Anthropic SDK used by every workflow node.

Nodes only depend on the `StructuredLLM` protocol, so tests can swap in a fake
and you can swap models per workflow without touching graph code.
"""
import base64
import mimetypes
from pathlib import Path
from typing import Protocol, Sequence, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Settings, settings as default_settings

T = TypeVar("T", bound=BaseModel)


IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class StructuredLLM(Protocol):
    def structured(self, system: str, prompt: str, schema: type[T], images: Sequence[str] = ()) -> T: ...


def _image_blocks(paths: Sequence[str]) -> list[dict]:
    blocks = []
    for p in paths:
        media_type = mimetypes.guess_type(p)[0]
        if media_type not in IMAGE_TYPES:
            continue
        data = base64.standard_b64encode(Path(p).read_bytes()).decode()
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    return blocks


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

    def structured(self, system: str, prompt: str, schema: type[T], images: Sequence[str] = ()) -> T:
        content = [*_image_blocks(images), {"type": "text", "text": prompt}]
        response = self.client.beta.messages.parse(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
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
