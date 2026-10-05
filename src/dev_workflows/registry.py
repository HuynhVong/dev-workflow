"""Every workflow the UI and CLI can run, found by discovery rather than a fixed list.

Sources, merged in this order (the first id wins):
1. built-ins: the devflow workflows;
2. `register_workflow(id, factory, ...)` calls, and `devflow.workflows` entry points in any installed package;
3. every graph in this repo's `langgraph.json`, plus extra `langgraph.json` files under `ui.graph_sources` in
   workspace.yaml.

A workflow is monitored with zero UI code: its graph comes from `get_graph()`, its start form from the graph's input
schema, its checkpoints from `interrupt()` payloads. A module may add polish with a `DEVFLOW_UI` dict (title,
description, icon, node labels, step order, a custom start form, `prepare(values, workspace) -> (label, inputs)`).
"""
import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
PALETTE = ["#3491ff", "#42cb80", "#eeb747", "#b78cff", "#ff8a65", "#4dd0e1", "#f06292", "#aed581"]


@dataclass
class WorkflowSpec:
    id: str
    factory: Callable[..., Any]          # (deps, checkpointer) for ticket workflows, (checkpointer) otherwise
    title: str = ""
    description: str = ""
    kind: str = "graph"                  # "ticket": devflow ticket workflow (worktrees, Jira, preflight); "graph": any graph
    source: str = "builtin"
    ui: dict = field(default_factory=dict)

    def meta(self, index: int = 0) -> dict:
        return {"id": self.id, "title": self.title or self.ui.get("title") or self.id.replace("_", " ").capitalize(),
                "description": self.description or self.ui.get("description", ""), "kind": self.kind, "source": self.source,
                "icon": self.ui.get("icon", "workflow"), "color": self.ui.get("color") or PALETTE[index % len(PALETTE)],
                "nodes": self.ui.get("nodes", {}), "node_details": self.ui.get("node_details", {}),
                "steps": self.ui.get("steps", []), "hidden_nodes": self.ui.get("hidden_nodes", []),
                "form": self.ui.get("form"), "checkpoints": self.ui.get("checkpoints", {})}


_registered: dict[str, WorkflowSpec] = {}


def register_workflow(id: str, factory: Callable[..., Any], *, title: str = "", description: str = "", kind: str = "graph",
                      ui: dict | None = None) -> None:
    """Make a graph runnable and monitorable from the UI and CLI. `factory(checkpointer)` returns the compiled graph
    (`factory(deps, checkpointer)` for kind="ticket")."""
    _registered[id] = WorkflowSpec(id, factory, title, description, kind, "register_workflow", ui or {})


def _builtins() -> list[WorkflowSpec]:
    from .jira_implement import address_review, graph, ticket_review
    from .workflows import standup, ticket_to_plan
    out = [WorkflowSpec(graph.WORKFLOW, lambda deps, cp: graph.build_graph(deps, checkpointer=cp), kind="ticket", ui=graph.DEVFLOW_UI),
           WorkflowSpec(address_review.WORKFLOW, lambda deps, cp: address_review.build_review_graph(deps, checkpointer=cp),
                        kind="ticket", ui=address_review.DEVFLOW_UI),
           WorkflowSpec(ticket_review.WORKFLOW, lambda deps, cp: ticket_review.build_graph(deps, checkpointer=cp),
                        kind="ticket", ui=ticket_review.DEVFLOW_UI)]
    # pr_review (diff in, Markdown out) is no longer a workflow of its own: it is the code review engine inside the
    # ticket review, jira ticket implement and address-review.
    for mod, wid in ((ticket_to_plan, "ticket_to_plan"), (standup, "standup")):
        out.append(WorkflowSpec(wid, (lambda m: lambda cp: m.build_graph(checkpointer=cp))(mod), ui=getattr(mod, "DEVFLOW_UI", {})))
    return out


def _entry_points() -> list[WorkflowSpec]:
    out = []
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="devflow.workflows"):
            try:
                obj = ep.load()
                spec = obj if isinstance(obj, WorkflowSpec) else _from_object(ep.name, obj, "entry_point")
                if spec:
                    out.append(spec)
            except Exception:  # noqa: BLE001 - one broken plugin never hides the others
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _from_object(wid: str, obj: Any, source: str, module=None) -> WorkflowSpec | None:
    """A compiled graph, or a module/function that builds one, as a workflow."""
    ui = getattr(module, "DEVFLOW_UI", {}) if module else {}
    build = getattr(module, "build_graph", None) if module else None
    if build is not None:
        return WorkflowSpec(wid, lambda cp: build(checkpointer=cp), source=source, ui=ui)
    if hasattr(obj, "get_graph") and hasattr(obj, "copy"):
        return WorkflowSpec(wid, lambda cp: obj.copy(update={"checkpointer": cp}), source=source, ui=ui)
    if callable(obj):
        return WorkflowSpec(wid, lambda cp: _with_checkpointer(obj(), cp), source=source, ui=ui)
    return None


def _with_checkpointer(g, cp):
    return g.copy(update={"checkpointer": cp}) if hasattr(g, "copy") else g


def _load_langgraph_json(path: Path) -> tuple[list[WorkflowSpec], list[str]]:
    specs, errors = [], []
    try:
        cfg = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"{path}: {e}"]
    for wid, target in (cfg.get("graphs") or {}).items():
        try:
            file, _, attr = str(target).partition(":")
            module_path = (path.parent / file).resolve()
            module = _import_file(module_path)
            obj = getattr(module, attr or "graph")
            spec = _from_object(wid, obj, f"langgraph.json ({path})", module)
            if spec:
                specs.append(spec)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{wid} in {path}: {type(e).__name__}: {e}")
    return specs, errors


def _import_file(path: Path):
    """Import a graph file as its package module when it lives in this package, else as a standalone module."""
    try:
        rel = path.relative_to(REPO_ROOT / "src")
        return importlib.import_module(".".join(rel.with_suffix("").parts))
    except ValueError:
        pass
    name = "devflow_graph_" + "_".join(path.with_suffix("").parts[-3:])
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Registry:
    def __init__(self, graph_sources: list[str] | None = None, base: Path | None = None):
        self.graph_sources = graph_sources or []
        self.base = base or Path.cwd()
        self.specs: dict[str, WorkflowSpec] = {}
        self.errors: list[str] = []
        self.refresh()

    def refresh(self) -> None:
        specs: dict[str, WorkflowSpec] = {}
        errors: list[str] = []
        found = _builtins() + list(_registered.values()) + _entry_points()
        files = [REPO_ROOT / "langgraph.json", Path.cwd() / "langgraph.json"]
        for src in self.graph_sources:
            p = Path(src).expanduser()
            p = p if p.is_absolute() else self.base / p
            files.append(p / "langgraph.json" if p.is_dir() else p)
        seen_files = set()
        for f in files:
            if f.exists() and f.resolve() not in seen_files:
                seen_files.add(f.resolve())
                more, errs = _load_langgraph_json(f)
                found += more
                errors += errs
        for s in found:
            specs.setdefault(s.id, s)
        self.specs, self.errors = specs, errors

    def get(self, wid: str) -> WorkflowSpec:
        if wid not in self.specs:
            raise KeyError(f"unknown workflow {wid!r}; known: {sorted(self.specs)}")
        return self.specs[wid]

    def list(self) -> list[WorkflowSpec]:
        return list(self.specs.values())
