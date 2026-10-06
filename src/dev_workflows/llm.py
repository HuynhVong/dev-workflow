"""Thin wrapper around Claude used by every workflow node: the Anthropic SDK with an API key, or headless Claude
Code on the developer's Claude Pro/Max login (see config.llm_backend).

Nodes only depend on the `StructuredLLM` protocol, so tests can swap in a fake
and you can swap models per workflow without touching graph code.
"""
import base64
import json
import mimetypes
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Protocol, Sequence, TypeVar

import anthropic
from pydantic import BaseModel

from . import telemetry
from .config import Settings, llm_backend, settings as default_settings
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
        started = time.time()
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
        telemetry.record_usage(step or "default", getattr(response, "model", None) or model,
                               telemetry.usage_dict(getattr(response, "usage", None)), duration_s=time.time() - started)
        if response.stop_reason == "refusal":
            raise LLMRefusal(f"Claude declined this request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(f"No structured output (stop_reason={response.stop_reason})")
        return response.parsed_output


# A structured call needs no MCP server and no skill listing. Without these flags Claude Code sends the definitions of
# every MCP tool the developer has installed with every call (`--tools ""` only drops the built-in tools), which can be
# tens of thousands of tokens per call.
NO_EXTRAS = ("--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands")


class ClaudeCliLLM:
    """Runs each structured call through headless Claude Code (`claude -p --json-schema`), so the AI steps use the
    developer's Claude Pro/Max login instead of a paid API key. Same models, effort and skills as ClaudeLLM."""

    def __init__(self, cfg: Settings = default_settings, routing: Routing | None = None, run=subprocess.run,
                 binary: str = "claude", timeout_s: int = 900):
        self.cfg = cfg
        self.routing = routing or Routing()
        self.run, self.binary, self.timeout_s = run, binary, timeout_s

    def structured(self, system: str, prompt: str, schema: type[T], images: Sequence[str] = (), step: str = "") -> T:
        model = self.routing.model(step) if step else self.cfg.model
        effort = self.routing.effort(model)
        if step:
            system += skills_system_block(self.routing.skills(step))
        images = [str(Path(p).resolve()) for p in images if mimetypes.guess_type(p)[0] in IMAGE_TYPES]
        if images:
            # Claude Code reads images with its Read tool, the only tool it gets.
            prompt += "\n\nRead these images with the Read tool before answering:\n" + "\n".join(f"- {p}" for p in images)
        cmd = [self.binary, "-p", "--output-format", "json", "--json-schema", json.dumps(schema.model_json_schema()),
               "--model", model, "--system-prompt", system, "--tools", "Read" if images else "",
               "--no-session-persistence", *NO_EXTRAS, *(["--effort", effort] if effort else [])]
        if images:
            cmd += ["--allowedTools", "Read", "--add-dir", *sorted({str(Path(p).parent) for p in images})]
        # Without the key Claude Code falls back to the logged-in Claude subscription, which is the point of this backend.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="devflow-llm-") as cwd:  # keeps repo CLAUDE.md files out of the call
            proc = self.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=self.timeout_s)
        try:
            out = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            raise RuntimeError(f"Claude Code failed (exit {proc.returncode}): {(proc.stderr or proc.stdout or '').strip()[:500]}") from None
        used = next(iter(out.get("modelUsage") or {}), None) or model
        telemetry.record_usage(step or "default", used, telemetry.usage_dict(out.get("usage")), source="claude_code",
                               duration_s=time.time() - started)
        if out.get("is_error") or out.get("subtype") != "success":
            raise RuntimeError(f"Claude Code failed: {str(out.get('result') or out.get('subtype'))[:500]}")
        if out.get("stop_reason") == "refusal":
            raise LLMRefusal(f"Claude declined this request: {out.get('result')}")
        if out.get("structured_output") is None:
            raise RuntimeError(f"No structured output from Claude Code (stop_reason={out.get('stop_reason')})")
        return schema.model_validate(out["structured_output"])


def make_llm(routing: Routing | None = None, cfg: Settings = default_settings) -> StructuredLLM:
    """ClaudeLLM when an API key is configured (or DEVFLOW_LLM_BACKEND=api), otherwise Claude Code on the Pro login."""
    return ClaudeLLM(cfg, routing=routing) if llm_backend(cfg) == "api" else ClaudeCliLLM(cfg, routing=routing)


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
        _default = make_llm()
    return _default
