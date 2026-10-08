"""Model helpers for the exercises.

- `structured(system, prompt, schema)`: one call that returns a validated Pydantic object. Uses devflow's backend, so it
  works with an ANTHROPIC_API_KEY or with your Claude Pro/Max login (`claude` CLI).
- `anthropic_model()`: a tool-calling model for the agent loops (exercises 02 and 07). It needs ANTHROPIC_API_KEY.
- `ScriptedModel`: replays a fixed script, so you can test your agent loop with no key and no cost.

A model is any callable `model(system, messages, tools) -> {"text": str, "tool_calls": [{"id","name","args"}]}`.
Messages are plain dicts: {"role": "user", "content": str}; {"role": "assistant", "content": str, "tool_calls": [...]};
{"role": "tool", "tool_call_id": str, "content": str}. A tool spec is {"name", "description", "parameters": JSON schema}."""
import os
from typing import Callable

MODEL_DEFAULT = "claude-sonnet-5-5"


def structured(system: str, prompt: str, schema, step: str = "ticket_to_plan.analyze"):
    """One structured call (see src/dev_workflows/llm.py). `step` only picks the model tier from devflow's routing table."""
    from dev_workflows.llm import make_llm
    return make_llm().structured(system, prompt, schema, step=step)


def anthropic_model(model: str = MODEL_DEFAULT, max_tokens: int = 1024) -> Callable:
    """A real tool-calling model on the Anthropic API (needs ANTHROPIC_API_KEY)."""
    import anthropic
    client = anthropic.Anthropic()

    def call(system, messages, tools):
        wire, pending = [], []
        for m in messages:
            if m["role"] == "tool":
                pending.append({"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]})
                continue
            if pending:
                wire.append({"role": "user", "content": pending})
                pending = []
            if m["role"] == "assistant":
                blocks = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [
                    {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]} for t in m.get("tool_calls", [])]
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": "user", "content": m["content"]})
        if pending:
            wire.append({"role": "user", "content": pending})
        r = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=wire,
                                   tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools])
        return {"text": "".join(b.text for b in r.content if b.type == "text"),
                "tool_calls": [{"id": b.id, "name": b.name, "args": dict(b.input)} for b in r.content if b.type == "tool_use"]}
    return call


class ScriptedModel:
    """Replays `script`, a list of model answers ({"text", "tool_calls"}); records every call it receives."""

    def __init__(self, script: list[dict]):
        self.script, self.calls = list(script), []

    def __call__(self, system, messages, tools):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": [t["name"] for t in tools]})
        if not self.script:
            raise AssertionError("the agent called the model more times than the script has answers")
        a = self.script.pop(0)
        return {"text": a.get("text", ""), "tool_calls": a.get("tool_calls", [])}


class Counted:
    """Wraps a model and counts calls and characters sent, a cheap stand-in for tokens when comparing designs."""

    def __init__(self, model: Callable):
        self.model, self.calls, self.chars = model, 0, 0

    def __call__(self, system, messages, tools):
        self.calls += 1
        self.chars += len(system) + sum(len(str(m.get("content", ""))) for m in messages)
        return self.model(system, messages, tools)
