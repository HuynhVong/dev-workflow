"""Run control for the CLI: start, drive checkpoints, resume, abort, status, audit."""
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .. import telemetry
from ..llm import make_llm
from ..routing import Routing
from .confluence import ConfluenceReader
from .address_review import WORKFLOW as REVIEW_WORKFLOW
from .address_review import build_review_graph
from . import worktrees
from .graph import WORKFLOW, Deps, PreflightFailed, build_graph
from .jira import JiraGateway
from .ledger import Store
from .workspace import Workspace, load_workspace


class RunConflict(RuntimeError):
    pass


def db_path(ws: Workspace) -> str:
    Path(ws.state_dir).mkdir(parents=True, exist_ok=True)
    return str(Path(ws.state_dir, "runs.sqlite"))


class MissingMcp:
    """Stands in for an MCP server workspace.yaml does not configure: every use fails with a clear message."""

    def __init__(self, name: str):
        self.name = name

    def list_tools(self):
        raise RuntimeError(f"MCP server '{self.name}' is not configured under mcp_servers in workspace.yaml "
                           "(nor, for Jira, connected to Claude Code)")

    call = lambda self, tool, args: self.list_tools()  # noqa: E731


def real_deps(ws: Workspace, store: Store) -> Deps:
    from .mcp_config import confluence_mcp_for, jira_mcp_for
    jira_mcp, _ = jira_mcp_for(ws)  # claude_code_mcp.jira, else workspace.yaml, else the Jira MCP in Claude Code's config
    jira_mcp = jira_mcp or MissingMcp(ws.jira_server)
    conf_mcp = confluence_mcp_for(ws, jira_mcp) or MissingMcp(ws.confluence_server)
    routing = Routing.from_config(ws.ai)
    return Deps(workspace=ws, store=store, llm=make_llm(routing), routing=routing,
                jira=JiraGateway(jira_mcp, ws.jira_tools, ws.status_order),
                confluence=ConfluenceReader(conf_mcp, ws.confluence_tools))


class Session:
    """Runs any discovered workflow (see dev_workflows.registry) under the run registry: lifecycle, audit, events and
    token usage. The CLI and the UI's run manager both drive runs through this class, so they produce the same history."""

    def __init__(self, ws: Workspace, deps_factory: Callable[[Workspace, Store], Deps] = real_deps,
                 ask: Callable[[dict], dict] | None = None, out=sys.stdout, registry=None):
        from ..registry import Registry
        self.ws = ws
        path = db_path(ws)
        self.store = Store(path)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        self.saver = SqliteSaver(conn)
        self.registry = registry or Registry(ws.ui.get("graph_sources"))
        self._deps_factory, self._deps = deps_factory, None
        self._graphs: dict[str, object] = {}
        self._lock = threading.Lock()
        self.cancelled: dict[str, str] = {}  # run_id -> note: stop at the next node boundary (UI abort of a running run)
        self.ask, self.out = ask, out

    @property
    def deps(self) -> Deps:
        with self._lock:
            if self._deps is None:
                self._deps = self._deps_factory(self.ws, self.store)
            return self._deps

    def graph_for(self, workflow: str):
        spec = self.registry.get(workflow)
        deps = self.deps if spec.uses_deps else None
        with self._lock:
            if workflow not in self._graphs:
                self._graphs[workflow] = spec.factory(deps, self.saver) if spec.uses_deps else spec.factory(self.saver)
            return self._graphs[workflow]

    def graph(self, run_id: str):
        return self.graph_for(self._run(run_id)["workflow"])

    @property
    def graphs(self) -> dict:
        return {w: self.graph_for(w) for w in (WORKFLOW, REVIEW_WORKFLOW)}

    def cfg(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def say(self, text: str) -> None:
        print(text, file=self.out)

    # ---------------------------------------------------------------- commands
    def create(self, workflow: str, values: dict) -> tuple[str, dict]:
        """Register a new PENDING run of any workflow (nothing runs yet). Returns (run_id, graph inputs)."""
        spec = self.registry.get(workflow)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if spec.kind == "ticket":
            ticket = str(values.get("ticket") or "").strip().upper()
            if not TICKET_KEY.match(ticket):
                raise ValueError(f"'{ticket}' is not a Jira key like AQS-5512")
            repos = [r for r in (values.get("repos") or []) if r]
            extra = spec.ui["inputs"](values, self.ws) if callable(spec.ui.get("inputs")) else {}
            busy = self.store.active_runs(ticket)
            if busy:  # one active run per ticket: they would share the ticket's worktrees
                b = busy[0]
                raise RunConflict(f"{ticket} already has an unfinished run {b['run_id']} ({b['status']}). Finish, resume or abort "
                                  f"it first (`devflow resume {b['run_id']}` / `devflow abort {b['run_id']}`).")
            kind = "review" if workflow == REVIEW_WORKFLOW else spec.ui.get("run_prefix", "")
            run_id = f"{ticket}-{kind + '-' if kind else ''}{stamp}"
            inputs = {"run_id": run_id, "ticket_key": ticket, "requested_repos": repos, **extra}
            self.store.create_run(run_id, workflow, ticket, label=ticket, inputs=inputs)
        else:
            prepare = spec.ui.get("prepare")
            label, inputs = prepare(values, self.ws) if prepare else (str(values.pop("_label", "") or spec.id), dict(values))
            run_id = f"{workflow}-{stamp}-{uuid.uuid4().hex[:4]}"
            if spec.uses_deps:  # devflow graphs track their own lifecycle by run_id
                inputs = {"run_id": run_id, **inputs}
            self.store.create_run(run_id, workflow, "", label=label, inputs=inputs)
        self.store.audit(run_id, "created", {"workflow": workflow, "values": _short(values)})
        return run_id, inputs

    def start(self, ticket: str, repos: list[str] | None, workflow: str = WORKFLOW) -> str:
        run_id, inputs = self.create(workflow, {"ticket": ticket, "repos": repos or []})
        self.say(f"Run {run_id} created.")
        return self.drive(run_id, inputs)

    def start_run(self, workflow: str, values: dict) -> tuple[str, str]:
        run_id, inputs = self.create(workflow, values)
        self.say(f"Run {run_id} created.")
        return run_id, self.drive(run_id, inputs)

    def resume(self, run_id: str, reopen: bool = False) -> str:
        run = self._run(run_id)
        if run["status"] == "COMPLETED":
            return self._done(run_id, "already COMPLETED")
        if run["status"] == "ABORTED":
            if not reopen:
                return self._done(run_id, "is ABORTED. Use `devflow resume --reopen` to continue it from the checkpoint where it was aborted.")
            snap = self.graph(run_id).get_state(self.cfg(run_id))
            at = snap.values.get("aborted_at") or run["checkpoint"]
            if at and self._spec(run_id).uses_deps:
                self.graph(run_id).update_state(self.cfg(run_id), {"aborted_at": ""}, as_node=at)
            elif not snap.next and not self._pending(snap):
                return self._done(run_id, "cannot be reopened: no checkpoint recorded")
            self.store.audit(run_id, "reopened", {"checkpoint": at})
        if run["status"] == "RUNNING" and not run["stale"]:
            return self._done(run_id, "is RUNNING in another process (heartbeat is fresh)")
        snap = self.graph(run_id).get_state(self.cfg(run_id))
        if snap.created_at is None:  # queued and never started: start it now
            return self.drive(run_id, self.store.inputs(run_id))
        if run["status"] == "WAITING_HUMAN" or (self._pending(snap) and not snap.next):
            pc = self._pending(snap)
            if pc:
                self.store.set_status(run_id, "WAITING_HUMAN", node=pc["name"], checkpoint=pc["name"])
                return "WAITING_HUMAN"
        return self.drive(run_id, None)

    def answer(self, run_id: str, ans: dict) -> str:
        run = self._run(run_id)
        pc = self._pending(self.graph(run_id).get_state(self.cfg(run_id)))
        if run["status"] != "WAITING_HUMAN" or not pc:
            return self._done(run_id, f"is not waiting at a checkpoint ({run['status']})")
        if pc.get("generic"):  # a plain interrupt() from any graph: the answer is a free value
            value = ans.get("value", ans.get("note", ""))
            self.store.audit(run_id, "decision", {"checkpoint": pc["name"], "value": value})
            self.store.set_status(run_id, "RUNNING", node=pc["name"])
            return self.drive(run_id, Command(resume=value))
        if ans.get("choice") not in pc["options"]:
            return self._done(run_id, f"is waiting at {pc['name']}: choose one of {pc['options']}")
        return self.drive(run_id, Command(resume=ans))

    def abort(self, run_id: str, note: str = "") -> str:
        run = self._run(run_id)
        if run["status"] == "WAITING_HUMAN" and self._spec(run_id).uses_deps:
            return self.drive(run_id, Command(resume={"choice": "abort", "note": note}))
        if run["status"] in ("FAILED", "PENDING", "WAITING_HUMAN") or (run["status"] == "RUNNING" and run["stale"]):
            self.store.set_status(run_id, "ABORTED", node=run["node"], checkpoint=run["checkpoint"], detail=note or "aborted")
            if run["ticket"] and not self.store.active_runs(run["ticket"]):
                try:
                    self.store.audit(run_id, "worktrees_released", worktrees.release(self.ws, run["ticket"]))
                except Exception:  # noqa: BLE001
                    pass
            return self._done(run_id, "ABORTED (no further steps will run)")
        return self._done(run_id, f"cannot be aborted while {run['status']}")

    def status(self, run_id: str) -> dict:
        run = self._run(run_id)
        snap = self.graph(run_id).get_state(self.cfg(run_id))
        v = snap.values
        return {**run, "next": list(snap.next), "repos": {r: {k: rs.get(k) for k in ("status", "fix_attempts_used", "branch")}
                                                          for r, rs in (v.get("repos") or {}).items()},
                "pending_checkpoint": self._pending(snap).get("name")}

    @staticmethod
    def _pending(snap) -> dict:
        """The checkpoint the run is paused at: the live interrupt first, then the state copy. A plain interrupt()
        (any graph) comes back as {name: <node>, payload, options: [], generic: True}."""
        for task in snap.tasks:
            for intr in task.interrupts:
                if isinstance(intr.value, dict) and intr.value.get("options"):
                    return intr.value
                return {"name": task.name, "payload": intr.value, "options": [], "generic": True}
        return snap.values.get("pending_checkpoint") or {}

    def show(self, run_id: str) -> dict:
        return {"run": self._run(run_id), "audit": self.store.audit_log(run_id), "side_effects": self.store.effects(run_id)}

    # ---------------------------------------------------------------- driving
    def drive(self, run_id: str, inp) -> str:
        """Run until the graph ends or waits for a human. Streams node start/finish events into the run's history."""
        cfg = self.cfg(run_id)
        telemetry.bind(run_id, self.store)
        while True:
            g = self.graph(run_id)
            interrupt_node, interrupt = "", None
            try:
                for mode, chunk in g.stream(inp, cfg, stream_mode=["tasks", "updates"]):
                    if mode == "tasks":
                        self._task_event(run_id, chunk)
                        if chunk.get("interrupts"):
                            interrupt_node = chunk["name"]
                    elif mode == "updates" and "__interrupt__" in chunk:
                        interrupt = chunk["__interrupt__"][0]
                    if run_id in self.cancelled:
                        break
            except PreflightFailed as e:
                self.store.set_status(run_id, "FAILED", node="preflight", detail="; ".join(e.gaps))
                self.say(str(e) + f"\nFix these, then run `devflow resume {run_id}`.")
                return "FAILED"
            except Exception as e:  # noqa: BLE001 - recorded, resumable
                node = (self.store.run(run_id) or {}).get("node", "")
                self.store.set_status(run_id, "FAILED", node=node, detail=f"{type(e).__name__}: {e}"[:1000])
                self.say(f"Run {run_id} FAILED at {node}: {e}\nIt is resumable: `devflow resume {run_id}`.")
                return "FAILED"
            if run_id in self.cancelled:
                note = self.cancelled.pop(run_id)
                node = self._run(run_id)["node"]
                self.store.set_status(run_id, "ABORTED", node=node, detail=note or "aborted while running")
                self.say(f"Run {run_id} ABORTED after {node}. Local branches and edits were left untouched.")
                return "ABORTED"
            if interrupt is None:
                status = self._run(run_id)["status"]
                if status not in ("COMPLETED", "ABORTED", "FAILED"):  # graphs that do not track their own lifecycle
                    self.store.set_status(run_id, "COMPLETED", node=self._run(run_id)["node"])
                output = g.get_state(cfg).values.get("output", "")
                if output:
                    self.store.event(run_id, "output", data={"text": str(output)[:50000]})
                self.say(str(output))
                return self._run(run_id)["status"]
            pc = interrupt.value if isinstance(interrupt.value, dict) and interrupt.value.get("options") else \
                {"name": interrupt_node or "interrupt", "payload": interrupt.value, "options": [], "generic": True}
            if self._run(run_id)["status"] != "WAITING_HUMAN" or pc.get("generic"):
                self.store.set_status(run_id, "WAITING_HUMAN", node=pc["name"], checkpoint=pc["name"])
            self.store.event(run_id, "checkpoint_waiting", node=pc["name"], data={"name": pc["name"], "options": pc["options"]})
            if self.ask is None:
                self.say(render_checkpoint(run_id, pc))
                if pc.get("generic"):
                    self.say(f"Answer with: devflow answer {run_id} --value '...'")
                else:
                    self.say(f"Answer with: devflow answer {run_id} --choice <{'|'.join(pc['options'])}> [--note ...]")
                return "WAITING_HUMAN"
            inp = Command(resume=self.ask(pc))

    def _task_event(self, run_id: str, chunk: dict) -> None:
        name = chunk.get("name", "")
        if name.startswith("__"):
            return
        inp = chunk.get("input")
        repo = inp.get("repo", "") if isinstance(inp, dict) and isinstance(inp.get("repo"), str) else ""
        if "input" in chunk:  # task started
            self.store.heartbeat(run_id, name)
            self.store.event(run_id, "node_started", node=name, repo=repo, data={"task": chunk.get("id")})
            return
        result, error = chunk.get("result"), chunk.get("error")
        if isinstance(result, dict) and isinstance(result.get("repos"), dict) and len(result["repos"]) == 1 and not repo:
            repo = next(iter(result["repos"]))
        data = {"task": chunk.get("id"), "error": str(error)[:2000] if error else None,
                "interrupted": bool(chunk.get("interrupts")), "result": trim(result)}
        self.store.event(run_id, "node_finished", node=name, repo=repo, data=data)
        if name == "fetch_ticket" and isinstance(result, dict) and (result.get("ticket") or {}).get("title"):
            self.store.set_label(run_id, f"{result['ticket'].get('key', '')}: {result['ticket']['title']}"[:200])

    def request_cancel(self, run_id: str, note: str = "") -> None:
        """Abort a run that is RUNNING in this process: it stops at the next node boundary, before later side effects."""
        self.cancelled[run_id] = note
        self.store.audit(run_id, "abort_requested", {"note": note})

    def _spec(self, run_id: str):
        return self.registry.get(self._run(run_id)["workflow"])

    def _run(self, run_id: str) -> dict:
        run = self.store.run(run_id)
        if not run:
            raise SystemExit(f"No run {run_id} in {db_path(self.ws)}")
        return run

    def _done(self, run_id: str, msg: str) -> str:
        self.say(f"Run {run_id} {msg}")
        return self._run(run_id)["status"]


TICKET_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
TRIM_CHARS = 20000


def jsonable(obj):
    """Graph state and node results as plain JSON (Pydantic models dumped, the rest stringified)."""
    try:
        from pydantic import BaseModel
    except ImportError:  # pragma: no cover
        BaseModel = ()  # type: ignore[assignment]

    def conv(o):
        if isinstance(o, BaseModel):
            return conv(o.model_dump())
        if isinstance(o, dict):
            return {str(k): conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple, set)):
            return [conv(v) for v in o]
        if o is None or isinstance(o, (str, int, float, bool)):
            return o
        return str(o)
    return conv(obj)


def trim(obj, limit: int = TRIM_CHARS):
    """A node result small enough for the events table; large values keep their head and are marked truncated."""
    data = jsonable(obj)
    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data
    return {"__truncated__": True, "preview": text[:limit]}


def _short(values: dict) -> dict:
    return {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in values.items()}


def render_checkpoint(run_id: str, pc: dict) -> str:
    return f"\n=== {run_id} is waiting for you at: {pc['name']} ===\n{json.dumps(pc['payload'], indent=2, default=str)}\nOptions: {', '.join(pc['options'])}"


def interactive_ask(pc: dict) -> dict:
    print(render_checkpoint("this run", pc))
    options = pc["options"]
    while True:
        c = input(f"Choose [{'/'.join(options)}]: ").strip()
        if c in options:
            break
    ans = {"choice": c}
    note = input("Note (optional, single line; use \\n for new lines): ").strip().replace("\\n", "\n")
    if note:
        ans["note"] = note
    name = pc["name"]
    if name == "clarify" and pc["payload"].get("scope_requests"):
        ans["approve_repos"] = _list(input("Approve adding which out-of-scope repos? (comma list, blank = none): "))
    if name == "triage" and c == "edit":
        for key in ("fix", "answer", "skip"):
            ans[key] = _list(input(f"Thread numbers to {key} (comma list, blank = keep the proposal): "))
    if (name == "route_ask" and c == "fix") or (name in ("manual_test", "manual_retest") and c == "feedback"):
        ans["repos"] = _list(input("Which repos? (comma list, blank = let the workflow decide): "))
    if name == "checkout_gate" and c == "ready":
        url = input(f"App URL (blank = {pc['payload'].get('app_url') or 'none'}): ").strip()
        if url:
            ans["app_url"] = url
    if name == "approve_test_plan" and c == "approve":
        ans["run"] = _list(input("Case ids to run (comma list, blank = all): "))
    if name == "review_results" and c == "retest":
        ans["retest"] = _list(input("Case ids to re-test (comma list, blank = the failed and skipped ones): "))
    if name == "confirm_tickets" and c == "continue":
        keep = _list(input("Ticket keys to keep (comma list, blank = all): "))
        if keep:
            ans["keep"] = keep
    if name == "review_report" and c == "edit":
        path = input("Path to a file with your edited report: ").strip()
        if path:
            ans["report"] = Path(path).expanduser().read_text()
    if name == "approve_comment" and c == "edit":
        path = input("Path to a file with your edited comment: ").strip()
        if path:
            ans["comment"] = Path(path).expanduser().read_text()
    return ans


def _list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def load(ws_path: str | None) -> Workspace:
    path = ws_path or os.getenv("DEVFLOW_WORKSPACE", "workspace.yaml")
    if not Path(path).expanduser().exists():
        raise SystemExit(f"workspace file not found: {path} (see workspace.example.yaml)")
    return load_workspace(path)
