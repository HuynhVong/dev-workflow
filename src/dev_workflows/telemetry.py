"""Token usage and coding-agent activity, recorded against the run and node that caused them.

Any graph's node runs inside LangGraph's run context, so `current()` reads the run id (the thread id) and the node
name from it: no node signature changes, and graphs that know nothing about devflow are covered too. The runner
binds each run id to its Store before driving it; outside a bound run nothing is recorded.
"""
import contextvars
import threading
from typing import Any

_stores: dict[str, Any] = {}
_lock = threading.Lock()
repo_var: contextvars.ContextVar[str] = contextvars.ContextVar("devflow_repo", default="")


def bind(run_id: str, store) -> None:
    with _lock:
        _stores[run_id] = store


def current() -> tuple[str, str]:
    """(run_id, node) of the LangGraph node running in this thread, or ("", "") outside a run."""
    try:
        from langgraph.config import get_config
        cfg = get_config()
    except Exception:  # noqa: BLE001 - not inside a graph
        return "", ""
    run_id = (cfg.get("configurable") or {}).get("thread_id") or ""
    node = (cfg.get("metadata") or {}).get("langgraph_node") or ""
    return str(run_id), str(node)


def _store(run_id: str):
    with _lock:
        return _stores.get(run_id)


def record_usage(step: str, model: str, usage: dict, source: str = "api", cost_usd: float | None = None,
                 duration_s: float | None = None, ctx: tuple[str, str] | None = None, repo: str = "") -> None:
    run_id, node = ctx or current()
    store = _store(run_id) if run_id else None
    if store is None:
        return
    try:
        store.record_usage(run_id, node, repo or repo_var.get(), step, model, source, usage, cost_usd, duration_s)
    except Exception:  # noqa: BLE001 - telemetry never breaks a run
        pass


def record_activity(data: dict, ctx: tuple[str, str] | None = None, repo: str = "") -> None:
    """One coding-agent action (a tool call, an edit, a denied command) for the live activity log."""
    run_id, node = ctx or current()
    store = _store(run_id) if run_id else None
    if store is None:
        return
    try:
        store.event(run_id, "agent_activity", node=node, repo=repo or repo_var.get(), data=data)
    except Exception:  # noqa: BLE001
        pass


def usage_dict(usage: Any) -> dict:
    """Anthropic SDK usage object or Agent SDK usage dict -> plain token counts."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        get = usage.get
    else:
        def get(k, default=None):
            return getattr(usage, k, default)
    return {k: int(get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
