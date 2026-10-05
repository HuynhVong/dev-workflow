"""Durable side-effect ledger + run registry (SQLite, same file as the LangGraph checkpoints).

Side effects follow intent -> act -> record. The intent is written BEFORE acting, outside the graph
checkpoint, so a crash between acting and checkpointing is visible on resume. Every effect also runs
its `detect` function first, so an effect that already happened is reconciled instead of repeated.
"""
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

LIFECYCLE = ("PENDING", "RUNNING", "WAITING_HUMAN", "FAILED", "ABORTED", "COMPLETED")
TERMINAL = ("ABORTED", "COMPLETED")
STALE_AFTER_S = 120


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")  # several runs (threads or processes) write at once
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devflow_runs (
              run_id TEXT PRIMARY KEY, workflow TEXT, ticket TEXT, status TEXT, node TEXT,
              checkpoint TEXT, detail TEXT, created_at REAL, updated_at REAL, heartbeat_at REAL);
            CREATE TABLE IF NOT EXISTS devflow_effects (
              id TEXT PRIMARY KEY, run_id TEXT, step TEXT, repo TEXT, action TEXT,
              status TEXT, result TEXT, created_at REAL, updated_at REAL);
            CREATE TABLE IF NOT EXISTS devflow_audit (
              run_id TEXT, at REAL, kind TEXT, data TEXT);
            """
        )

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- run registry -------------------------------------------------
    def create_run(self, run_id: str, workflow: str, ticket: str) -> None:
        now = time.time()
        self._exec("INSERT OR IGNORE INTO devflow_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (run_id, workflow, ticket, "PENDING", "", "", "", now, now, now))
        self.audit(run_id, "status", {"status": "PENDING"})

    def set_status(self, run_id: str, status: str, node: str = "", checkpoint: str = "", detail: str = "") -> None:
        assert status in LIFECYCLE, status
        now = time.time()
        self._exec("UPDATE devflow_runs SET status=?, node=COALESCE(NULLIF(?, ''), node), checkpoint=?, detail=?, updated_at=?, heartbeat_at=? WHERE run_id=?",
                   (status, node, checkpoint, detail, now, now, run_id))
        self.audit(run_id, "status", {"status": status, "node": node, "checkpoint": checkpoint, "detail": detail})

    def heartbeat(self, run_id: str, node: str) -> None:
        now = time.time()
        self._exec("UPDATE devflow_runs SET status='RUNNING', node=?, checkpoint='', heartbeat_at=?, updated_at=? WHERE run_id=? AND status NOT IN ('ABORTED','COMPLETED')",
                   (node, now, now, run_id))

    def run(self, run_id: str) -> dict | None:
        rows = self._exec("SELECT run_id, workflow, ticket, status, node, checkpoint, detail, created_at, updated_at, heartbeat_at FROM devflow_runs WHERE run_id=?", (run_id,))
        if not rows:
            return None
        keys = ("run_id", "workflow", "ticket", "status", "node", "checkpoint", "detail", "created_at", "updated_at", "heartbeat_at")
        r = dict(zip(keys, rows[0]))
        r["stale"] = r["status"] == "RUNNING" and time.time() - (r["heartbeat_at"] or 0) > STALE_AFTER_S
        return r

    def runs(self, limit: int = 20) -> list[dict]:
        ids = [r[0] for r in self._exec("SELECT run_id FROM devflow_runs ORDER BY updated_at DESC LIMIT ?", (limit,))]
        return [self.run(i) for i in ids]

    def active_runs(self, ticket: str) -> list[dict]:
        """Runs of this ticket that are not finished (a FAILED or stale run still owns its worktree)."""
        ids = [r[0] for r in self._exec("SELECT run_id FROM devflow_runs WHERE ticket=? AND status NOT IN ('ABORTED','COMPLETED')", (ticket,))]
        return [self.run(i) for i in ids]

    def audit(self, run_id: str, kind: str, data: dict) -> None:
        self._exec("INSERT INTO devflow_audit VALUES (?,?,?,?)", (run_id, time.time(), kind, json.dumps(data, default=str)))

    def audit_log(self, run_id: str) -> list[dict]:
        return [{"at": at, "kind": k, **json.loads(d)} for at, k, d in
                self._exec("SELECT at, kind, data FROM devflow_audit WHERE run_id=? ORDER BY at", (run_id,))]

    # --- side effects --------------------------------------------------
    def effect_status(self, effect_id: str) -> tuple[str, Any] | None:
        rows = self._exec("SELECT status, result FROM devflow_effects WHERE id=?", (effect_id,))
        return (rows[0][0], json.loads(rows[0][1]) if rows[0][1] else None) if rows else None

    def _effect_write(self, effect_id, run_id, step, repo, action, status, result=None):
        now = time.time()
        self._exec(
            "INSERT INTO devflow_effects VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, result=excluded.result, updated_at=excluded.updated_at",
            (effect_id, run_id, step, repo, action, status, json.dumps(result, default=str) if result is not None else None, now, now))

    def effects(self, run_id: str) -> list[dict]:
        rows = self._exec("SELECT id, step, repo, action, status, result FROM devflow_effects WHERE run_id=? ORDER BY created_at", (run_id,))
        return [dict(zip(("id", "step", "repo", "action", "status", "result"), r)) for r in rows]


@dataclass
class Effect:
    run_id: str
    step: str
    repo: str
    action: str

    @property
    def id(self) -> str:
        return f"{self.run_id}:{self.step}:{self.repo}:{self.action}"


def perform(store: Store, eff: Effect, detect: Callable[[], Any], act: Callable[[], Any]) -> dict:
    """Run a side effect at most once. `detect` returns the existing result (or None) by querying the real world."""
    prior = store.effect_status(eff.id)
    existing = detect()
    if existing is not None:
        status = "reconciled" if prior and prior[0] == "intent" else "done"
        store._effect_write(eff.id, eff.run_id, eff.step, eff.repo, eff.action, status, existing)
        return {"status": "skipped_existing", "result": existing}
    store._effect_write(eff.id, eff.run_id, eff.step, eff.repo, eff.action, "intent")
    result = act()
    store._effect_write(eff.id, eff.run_id, eff.step, eff.repo, eff.action, "done", result)
    return {"status": "done", "result": result}
