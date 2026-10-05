"""Run control for the CLI: start, drive checkpoints, resume, abort, status, audit."""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..llm import ClaudeLLM
from .confluence import ConfluenceReader
from .address_review import WORKFLOW as REVIEW_WORKFLOW
from .address_review import build_review_graph
from .graph import WORKFLOW, Deps, PreflightFailed, build_graph
from .jira import JiraGateway
from .ledger import Store
from .mcp_client import StdioOrHttpMcp
from .workspace import Workspace, load_workspace


def db_path(ws: Workspace) -> str:
    Path(ws.state_dir).mkdir(parents=True, exist_ok=True)
    return str(Path(ws.state_dir, "runs.sqlite"))


def real_deps(ws: Workspace, store: Store) -> Deps:
    jira_mcp = StdioOrHttpMcp(ws.jira_server, ws.mcp_servers[ws.jira_server])
    conf_mcp = jira_mcp if ws.confluence_server == ws.jira_server else StdioOrHttpMcp(ws.confluence_server, ws.mcp_servers[ws.confluence_server])
    return Deps(workspace=ws, store=store, llm=ClaudeLLM(),
                jira=JiraGateway(jira_mcp, ws.jira_tools, ws.status_order),
                confluence=ConfluenceReader(conf_mcp, ws.confluence_tools))


class Session:
    def __init__(self, ws: Workspace, deps_factory: Callable[[Workspace, Store], Deps] = real_deps,
                 ask: Callable[[dict], dict] | None = None, out=sys.stdout):
        self.ws = ws
        path = db_path(ws)
        self.store = Store(path)
        self.saver = SqliteSaver(sqlite3.connect(path, check_same_thread=False))
        deps = deps_factory(ws, self.store)
        self.graphs = {WORKFLOW: build_graph(deps, checkpointer=self.saver),
                       REVIEW_WORKFLOW: build_review_graph(deps, checkpointer=self.saver)}
        self.ask, self.out = ask, out

    def graph(self, run_id: str):
        return self.graphs[self._run(run_id)["workflow"]]

    def cfg(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def say(self, text: str) -> None:
        print(text, file=self.out)

    # ---------------------------------------------------------------- commands
    def start(self, ticket: str, repos: list[str] | None, workflow: str = WORKFLOW) -> str:
        run_id = f"{ticket}-{'review-' if workflow == REVIEW_WORKFLOW else ''}{time.strftime('%Y%m%d-%H%M%S')}"
        self.store.create_run(run_id, workflow, ticket)
        self.say(f"Run {run_id} created.")
        return self.drive(run_id, {"run_id": run_id, "ticket_key": ticket, "requested_repos": repos or []})

    def resume(self, run_id: str, reopen: bool = False) -> str:
        run = self._run(run_id)
        if run["status"] == "COMPLETED":
            return self._done(run_id, "already COMPLETED")
        if run["status"] == "ABORTED":
            if not reopen:
                return self._done(run_id, "is ABORTED. Use `devflow resume --reopen` to continue it from the checkpoint where it was aborted.")
            at = self.graph(run_id).get_state(self.cfg(run_id)).values.get("aborted_at") or run["checkpoint"]
            if not at:
                return self._done(run_id, "cannot be reopened: no checkpoint recorded")
            self.graph(run_id).update_state(self.cfg(run_id), {"aborted_at": ""}, as_node=at)
            self.store.audit(run_id, "reopened", {"checkpoint": at})
        if run["status"] == "RUNNING" and not run["stale"]:
            return self._done(run_id, "is RUNNING in another process (heartbeat is fresh)")
        return self.drive(run_id, None)

    def answer(self, run_id: str, ans: dict) -> str:
        run = self._run(run_id)
        pc = self._pending(self.graph(run_id).get_state(self.cfg(run_id)))
        if run["status"] != "WAITING_HUMAN" or not pc:
            return self._done(run_id, f"is not waiting at a checkpoint ({run['status']})")
        if ans.get("choice") not in pc["options"]:
            return self._done(run_id, f"is waiting at {pc['name']}: choose one of {pc['options']}")
        return self.drive(run_id, Command(resume=ans))

    def abort(self, run_id: str, note: str = "") -> str:
        run = self._run(run_id)
        if run["status"] == "WAITING_HUMAN":
            return self.drive(run_id, Command(resume={"choice": "abort", "note": note}))
        if run["status"] in ("FAILED", "PENDING") or (run["status"] == "RUNNING" and run["stale"]):
            self.store.set_status(run_id, "ABORTED", node=run["node"], detail=note or "aborted from the CLI")
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
        """The checkpoint the run is paused at: the live interrupt first, then the state copy."""
        for task in snap.tasks:
            for intr in task.interrupts:
                if isinstance(intr.value, dict) and intr.value.get("options"):
                    return intr.value
        return snap.values.get("pending_checkpoint") or {}

    def show(self, run_id: str) -> dict:
        return {"run": self._run(run_id), "audit": self.store.audit_log(run_id), "side_effects": self.store.effects(run_id)}

    # ---------------------------------------------------------------- driving
    def drive(self, run_id: str, inp) -> str:
        cfg = self.cfg(run_id)
        while True:
            try:
                out = self.graph(run_id).invoke(inp, cfg)
            except PreflightFailed as e:
                self.store.set_status(run_id, "FAILED", node="preflight", detail="; ".join(e.gaps))
                self.say(str(e) + f"\nFix these, then run `devflow resume {run_id}`.")
                return "FAILED"
            except Exception as e:  # noqa: BLE001 - recorded, resumable
                node = (self.store.run(run_id) or {}).get("node", "")
                self.store.set_status(run_id, "FAILED", node=node, detail=f"{type(e).__name__}: {e}"[:1000])
                self.say(f"Run {run_id} FAILED at {node}: {e}\nIt is resumable: `devflow resume {run_id}`.")
                return "FAILED"
            if "__interrupt__" not in out:
                self.say(out.get("output", ""))
                return self._run(run_id)["status"]
            pc = out["__interrupt__"][0].value
            if self.ask is None:
                self.say(render_checkpoint(run_id, pc))
                self.say(f"Answer with: devflow answer {run_id} --choice <{'|'.join(pc['options'])}> [--note ...]")
                return "WAITING_HUMAN"
            inp = Command(resume=self.ask(pc))

    def _run(self, run_id: str) -> dict:
        run = self.store.run(run_id)
        if not run:
            raise SystemExit(f"No run {run_id} in {db_path(self.ws)}")
        return run

    def _done(self, run_id: str, msg: str) -> str:
        self.say(f"Run {run_id} {msg}")
        return self._run(run_id)["status"]


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
    return ans


def _list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def load(ws_path: str | None) -> Workspace:
    path = ws_path or os.getenv("DEVFLOW_WORKSPACE", "workspace.yaml")
    if not Path(path).expanduser().exists():
        raise SystemExit(f"workspace file not found: {path} (see workspace.example.yaml)")
    return load_workspace(path)
