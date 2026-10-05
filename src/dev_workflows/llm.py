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
from .routing import Routing, skills_system_block

T = TypeVar("T", bound=BaseModel)


IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class StructuredLLM(Protocol):
    def structured(self, system: str, prompt: str, schema: type[T], images: Sequence[str] = (), step: str = "") -> T: ...


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
    def __init__(self, cfg: Settings = default_settings, client: anthropic.Anthropic | None = None,
                 routing: Routing | None = None):
        self.cfg = cfg
        self._client = client
        self.routing = routing or Routing()

    @property
    def client(self) -> anthropic.Anthropic:
        # Created lazily so importing a graph never needs an API key.
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def structured(self, system: str, prompt: str, schema: type[T], images: Sequence[str] = (), step: str = "") -> T:
        """`step` picks the model (routing.STEPS) and the global skills added to the system prompt."""
        content = [*_image_blocks(images), {"type": "text", "text": prompt}]
        model = self.routing.model(step) if step else self.cfg.model
        effort = self.routing.effort(model)
        if step:
            system += skills_system_block(self.routing.skills(step))
        response = self.client.beta.messages.parse(
            model=model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            **({"output_config": {"effort": effort}} if effort else {}),
            # Server-side fallback: if a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise LLMRefusal(f"Claude declined this request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(f"No structured output (stop_reason={response.stop_reason})")
        return response.parsed_output


class AsStep:
    """Runs a reused graph's LLM calls as one step of the calling workflow (e.g. pr_review inside repo_review)."""

    def __init__(self, llm: StructuredLLM, step: str):
        self.llm, self.step = llm, step

    def structured(self, system: str, prompt: str, schema: type[T], images: Sequence[str] = (), step: str = "") -> T:
        return self.llm.structured(system, prompt, schema, images=images, step=self.step)


_default: StructuredLLM | None = None


def default_llm() -> StructuredLLM:
    global _default
    if _default is None:
        _default = ClaudeLLM()
    return _default
