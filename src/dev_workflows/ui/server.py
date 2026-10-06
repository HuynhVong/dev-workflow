"""`devflow ui`: a local web app over the same runner the CLI uses (design: docs/devflow-ui-plan.md).

Local only: bound to 127.0.0.1, a random token in the launch URL becomes a cookie, the Host header is checked against
DNS rebinding, and there is no CORS. Secrets never reach the browser (workspace.yaml values are masked).
"""
import asyncio
import json
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .. import doctor
from ..jira_implement import task_inputs, worktrees
from ..jira_implement.runner import RunConflict, Session, jsonable, trim
from ..jira_implement.workspace import MASK, masked, read_raw, save_raw
from ..routing import Routing
from .manager import RunManager

STATIC = Path(__file__).parent / "static"
ACTIVE = ("PENDING", "RUNNING", "WAITING_HUMAN", "FAILED")
ANSWER_KEYS = ("choice", "note", "repos", "approve_repos", "fix", "answer", "skip", "value",
               "app_url", "run", "cases", "retest", "comment")  # the last five: ticket review checkpoints


class App:
    """Everything the HTTP layer needs; the session can be swapped when workspace.yaml changes."""

    def __init__(self, session_factory, workspace_path: str, token: str | None = None, port: int = 0):
        self.session_factory = session_factory
        self.workspace_path = workspace_path
        self.token = token or secrets.token_urlsafe(24)
        self.port = port
        self.lock = threading.Lock()
        self.doctor_result: dict | None = None
        self.session: Session | None = None
        self.manager: RunManager | None = None
        self.load_error = ""
        self.reload()

    def reload(self) -> None:
        with self.lock:
            if self.manager:
                self.manager.shutdown()
            try:
                self.session = self.session_factory()
                self.manager = RunManager(self.session)
                self.load_error = ""
            except SystemExit as e:  # workspace.yaml missing: the setup wizard creates it
                self.session, self.manager, self.load_error = None, None, str(e)
            except Exception as e:  # noqa: BLE001
                self.session, self.manager, self.load_error = None, None, f"{type(e).__name__}: {e}"

    def need(self) -> tuple[Session, RunManager]:
        if self.session is None or self.manager is None:
            raise HTTPException(409, {"error": "setup_required", "detail": self.load_error or "workspace.yaml not loaded"})
        return self.session, self.manager


def create_app(state: App) -> FastAPI:
    api = FastAPI(title="devflow", docs_url=None, redoc_url=None, openapi_url=None)

    @api.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"error": "bad host"}, status_code=403)
        token = request.query_params.get("token")
        if token and secrets.compare_digest(token, state.token):
            resp = RedirectResponse(request.url.path or "/", status_code=303)
            resp.set_cookie("devflow_token", state.token, httponly=True, samesite="strict")
            return resp
        cookie = request.cookies.get("devflow_token") or ""
        if not secrets.compare_digest(cookie, state.token):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return HTMLResponse("<p style='font-family:sans-serif'>Open devflow with the link <code>devflow ui</code> printed.</p>",
                                status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------------ meta, doctor, workspace
    @api.get("/api/meta")
    def meta():
        ws = state.session.ws if state.session else None
        s = state.manager.slots() if state.manager else {"max": 0, "running": 0, "queued": 0}
        return {"workspace_path": state.workspace_path, "workspace_exists": Path(state.workspace_path).expanduser().exists(),
                "loaded": state.session is not None, "load_error": state.load_error, "slots": s,
                "worktree_root": ws.worktree_root if ws else "", "state_dir": ws.state_dir if ws else "",
                "repos": sorted(ws.repos) if ws else [], "doctor": (state.doctor_result or {}).get("summary"),
                "user": os.environ.get("USER", "")}

    @api.get("/api/doctor")
    def get_doctor():
        return state.doctor_result or run_doctor({})

    @api.post("/api/doctor/run")
    def run_doctor(body: dict = Body(default={})):
        session, _ = state.need()
        repos = body.get("repos") or None
        checks = doctor.run(session.ws, deps=_deps_or_none(session), repos=repos, workflow=body.get("workflow") or "")
        if body.get("ping_models"):
            checks += doctor.ping_models(Routing.from_config(session.ws.ai))
        result = {"checks": [c.to_dict() for c in checks], "summary": doctor.summary(checks), "at": time.time()}
        if not repos and not body.get("workflow"):
            state.doctor_result = result
        return result

    @api.get("/api/workspace")
    def get_workspace():
        raw = read_raw(state.workspace_path)
        data, refs = masked(raw)
        import yaml
        return {"path": state.workspace_path, "exists": Path(state.workspace_path).expanduser().exists(), "data": data,
                "env_refs": refs, "mask": MASK, "yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if data else ""}

    @api.put("/api/workspace")
    def put_workspace(body: dict = Body(...)):
        data = body.get("data")
        if not isinstance(data, dict):
            raise HTTPException(400, {"error": "invalid", "detail": "data must be a mapping"})
        if state.manager and any(state.manager.is_active(r) for r in state.manager.threads):
            raise HTTPException(409, {"error": "busy", "detail": "a run is working; save when no run is RUNNING"})
        try:
            save_raw(state.workspace_path, data)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, {"error": "invalid", "detail": f"{type(e).__name__}: {e}"}) from e
        state.reload()
        state.doctor_result = None
        return get_workspace()

    @api.get("/api/skills")
    def skills():
        session, _ = state.need()
        r = Routing.from_config(session.ws.ai)
        return {"dirs": [str(d) for d in r.registry.dirs],
                "installed": [{"name": s.name, "description": s.description} for s in r.registry.skills.values()],
                "steps": [{"step": n, "model": r.model(n), "tier": st.model, "skills": list(st.skills), "stack": st.stack,
                           "domain": st.domain, "escalate_to": st.escalate_to, "missing": r.missing().get(n, [])}
                          for n, st in r.steps.items()],
                "models": r.models, "effort": "medium"}

    # ------------------------------------------------------------------ workflows
    @api.get("/api/workflows")
    def workflows():
        session, _ = state.need()
        out = []
        for i, spec in enumerate(session.registry.list()):
            m = spec.meta(i)
            try:
                g = session.graph_for(spec.id)
                m["graph"] = graph_json(g)
                m["input_schema"] = g.get_input_jsonschema()
                if not m["form"]:
                    m["form"] = form_from_schema(m["input_schema"])
                    m["generated_form"] = True
            except Exception as e:  # noqa: BLE001
                m["error"] = f"{type(e).__name__}: {e}"
            out.append(m)
        return {"workflows": out, "errors": session.registry.errors}

    @api.post("/api/workflows/refresh")
    def refresh_workflows():
        session, _ = state.need()
        session.registry.refresh()
        with session._lock:
            session._graphs.clear()
        return workflows()

    # ------------------------------------------------------------------ runs
    @api.get("/api/runs")
    def runs(limit: int = 300):
        session, manager = state.need()
        usage = _usage_totals(session)
        out = []
        for r in session.store.all_runs(limit):
            item = {**r, "queued": r["run_id"] in manager.queued(), "active": manager.is_active(r["run_id"]),
                    "tokens": usage.get(r["run_id"], {})}
            if r["status"] in ACTIVE:
                item.update(_live(session, r))
            out.append(item)
        return {"runs": out, "slots": manager.slots()}

    @api.post("/api/runs")
    def start_run(body: dict = Body(...)):
        session, manager = state.need()
        try:
            run_id = manager.start(body.get("workflow", ""), dict(body.get("values") or {}))
        except RunConflict as e:
            raise HTTPException(409, {"error": "conflict", "detail": str(e)}) from e
        except (ValueError, KeyError) as e:
            raise HTTPException(400, {"error": "invalid", "detail": str(e).strip("'\"")}) from e
        return {"run_id": run_id, "queued": run_id in manager.queued()}

    @api.post("/api/uploads")
    def upload(body: dict = Body(...)):
        """One image for a start form: {name, data} with data base64 (a data: URL is fine). Returns {id, name, size}; the
        form sends the ids back as `images` and the run keeps its own copies."""
        session, _ = state.need()
        try:
            return task_inputs.save_upload(session.ws, str(body.get("name") or "image"), str(body.get("data") or ""))
        except task_inputs.InputError as e:
            raise HTTPException(400, {"error": "invalid", "detail": str(e)}) from e

    @api.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        session, manager = state.need()
        r = _run_or_404(session, run_id)
        detail = {**r, **_live(session, r, full=True), "queued": run_id in manager.queued(), "active": manager.is_active(run_id),
                  "tokens": _usage_totals(session, run_id).get(run_id, {}), "inputs": session.store.inputs(run_id)}
        return detail

    @api.post("/api/runs/{run_id}/answer")
    def answer(run_id: str, body: dict = Body(...)):
        session, manager = state.need()
        r = _run_or_404(session, run_id)
        pc = session._pending(session.graph(run_id).get_state(session.cfg(run_id)))
        if r["status"] != "WAITING_HUMAN" or not pc:
            raise HTTPException(409, {"error": "not_waiting", "detail": f"{run_id} is not waiting at a checkpoint ({r['status']})"})
        if not pc.get("generic") and body.get("choice") not in pc["options"]:
            raise HTTPException(400, {"error": "invalid", "detail": f"choose one of {pc['options']}"})
        ans = {k: v for k, v in body.items() if k in ANSWER_KEYS and v not in (None, "", [])}
        try:
            manager.answer(run_id, ans)
        except RuntimeError as e:
            raise HTTPException(409, {"error": "busy", "detail": str(e)}) from e
        return {"ok": True}

    @api.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, body: dict = Body(default={})):
        session, manager = state.need()
        r = _run_or_404(session, run_id)
        if r["status"] == "RUNNING" and not r["stale"]:
            raise HTTPException(409, {"error": "running", "detail": f"{run_id} is already running"})
        if r["status"] == "ABORTED" and not body.get("reopen"):
            raise HTTPException(409, {"error": "aborted", "detail": "ABORTED runs are reopened explicitly (reopen: true)"})
        if r["status"] == "COMPLETED":
            raise HTTPException(409, {"error": "completed", "detail": "already COMPLETED"})
        try:
            manager.resume(run_id, reopen=bool(body.get("reopen")))
        except RuntimeError as e:
            raise HTTPException(409, {"error": "busy", "detail": str(e)}) from e
        return {"ok": True}

    @api.post("/api/runs/{run_id}/abort")
    def abort(run_id: str, body: dict = Body(default={})):
        session, manager = state.need()
        r = _run_or_404(session, run_id)
        if r["status"] in ("ABORTED", "COMPLETED"):
            raise HTTPException(409, {"error": "finished", "detail": f"{run_id} is already {r['status']}"})
        if r["status"] == "RUNNING" and not manager.is_active(run_id) and not r["stale"]:
            raise HTTPException(409, {"error": "elsewhere", "detail": "this run is working in another process (the CLI); abort it there"})
        if r["status"] == "WAITING_HUMAN" and session._spec(run_id).uses_deps:
            manager.answer(run_id, {"choice": "abort", "note": body.get("note", "")})
            return {"status": "ABORTING"}
        return {"status": manager.abort(run_id, body.get("note", ""))}

    @api.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0, limit: int = 5000):
        session, _ = state.need()
        _run_or_404(session, run_id)
        return {"events": session.store.events(run_id, after=after, limit=limit)}

    @api.get("/api/runs/{run_id}/audit")
    def audit(run_id: str):
        session, _ = state.need()
        _run_or_404(session, run_id)
        return {"audit": session.store.audit_log(run_id), "side_effects": session.store.effects(run_id)}

    @api.get("/api/runs/{run_id}/nodes/{seq}")
    def node_detail(run_id: str, seq: int):
        """One node execution: its output, the state it received, the agent's activity and the tokens it used."""
        session, _ = state.need()
        _run_or_404(session, run_id)
        evs = session.store.events(run_id, after=0, limit=100000)
        fin = next((e for e in evs if e["seq"] == seq), None)
        if not fin or fin["kind"] not in ("node_finished", "node_started"):
            raise HTTPException(404, {"error": "not_found"})
        task = (fin["data"] or {}).get("task")
        started = next((e for e in evs if e["kind"] == "node_started" and (e["data"] or {}).get("task") == task and e["seq"] <= seq), fin)
        finished = next((e for e in evs if e["kind"] == "node_finished" and (e["data"] or {}).get("task") == task and e["seq"] >= started["seq"]), None)
        hi = finished["seq"] if finished else 1 << 62
        window = [e for e in evs if started["seq"] <= e["seq"] <= hi and e["node"] == started["node"]
                  and (not started["repo"] or e["repo"] in ("", started["repo"]))]
        return {"node": started["node"], "repo": started["repo"], "started": started, "finished": finished,
                "activity": [e for e in window if e["kind"] == "agent_activity"],
                "usage": [e for e in window if e["kind"] == "usage"], "input": _node_input(session, run_id, task)}

    @api.get("/api/runs/{run_id}/diff")
    def diff(run_id: str, repo: str):
        session, _ = state.need()
        r = _run_or_404(session, run_id)
        values = session.graph(run_id).get_state(session.cfg(run_id)).values
        scope = values.get("scope") or {}
        if repo not in scope:
            raise HTTPException(404, {"error": "not_found", "detail": f"{repo} is not in this run's scope"})
        if not Path(scope[repo]).exists():
            return {"repo": repo, "diff": "", "files": [], "note": "the worktree no longer exists"}
        from ..jira_implement.scope import ScopeGuard
        from ..jira_implement.vcs import Vcs
        vcs = Vcs(ScopeGuard.create({repo: scope[repo]}), prefix=session.ws.vcs_prefix,
                  **({"runner": session.deps.vcs_runner} if session.deps.vcs_runner else {}))
        base = session.ws.repos[repo].base_branch if repo in session.ws.repos else "develop"
        try:
            if values.get("commits"):  # ticket review: the reviewed commits, read from your own clone without touching it
                text = "\n\n".join(vcs.show_commit(repo, sha) for sha in values["commits"].get(repo, []))
            else:
                text = vcs.diff_against(repo, f"origin/{base}")
        except Exception as e:  # noqa: BLE001
            return {"repo": repo, "diff": "", "files": [], "note": f"{type(e).__name__}: {e}"}
        files = [l[len("diff --git a/"):].split(" b/")[0] for l in text.splitlines() if l.startswith("diff --git a/")]
        return {"repo": repo, "diff": text[:2_000_000], "files": files, "path": scope[repo], "ticket": r["ticket"]}

    @api.get("/api/runs/{run_id}/file")
    def run_file(run_id: str, path: str):
        """A file a run wrote under its own folder in state_dir (e.g. a ticket review's proof screenshots)."""
        session, _ = state.need()
        _run_or_404(session, run_id)
        root = Path(session.ws.state_dir, run_id).resolve()
        target = Path(path)
        target = (target if target.is_absolute() else root / target).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, {"error": "not_found", "detail": "no such file in this run's folder"})
        return FileResponse(target)

    @api.get("/api/runs/{run_id}/usage")
    def run_usage(run_id: str):
        session, _ = state.need()
        _run_or_404(session, run_id)
        rows = [_with_cost(session, u) for u in session.store.usage(run_id)]
        return {"rows": rows, "totals": _sum(rows), "by_node": _group(rows, "node"), "by_model": _group(rows, "model")}

    @api.get("/api/usage")
    def usage(since: float = 0, until: float | None = None):
        session, _ = state.need()
        runs = {r["run_id"]: r for r in session.store.all_runs(100000)}
        rows = [_with_cost(session, u) for u in session.store.usage(since=since, until=until)]
        for u in rows:
            u["day"] = time.strftime("%Y-%m-%d", time.localtime(u["at"]))
            u["workflow"] = (runs.get(u["run_id"]) or {}).get("workflow", "")
        by_step = sorted(_group(rows, "step"), key=lambda g: -(g["cost_usd"] or 0) - g["input_tokens"] / 1e9)
        return {"totals": _sum(rows), "by_day": _group(rows, "day"), "by_workflow": _group(rows, "workflow"),
                "by_model": _group(rows, "model"), "by_step": by_step[:15], "priced_models": sorted(session.ws.prices)}

    # ------------------------------------------------------------------ worktrees
    @api.get("/api/worktrees")
    def list_worktrees():
        session, _ = state.need()
        runs = session.store.all_runs(100000)
        out = []
        for w in worktrees.list_all(session.ws, session.deps.vcs_runner if session._deps else None):
            mine = [r for r in runs if r["ticket"] == w["ticket"]]
            active = next((r for r in mine if r["status"] in ACTIVE), None)
            w["run"] = (active or (mine[0] if mine else None))
            w["can_clean"] = not active and w["dirty"] is False
            out.append(w)
        return {"worktrees": out, "root": session.ws.worktree_root}

    @api.post("/api/worktrees/clean")
    def clean_worktree(body: dict = Body(...)):
        session, _ = state.need()
        ticket, repo = body.get("ticket", ""), body.get("repo", "")
        if session.store.active_runs(ticket):
            raise HTTPException(409, {"error": "active", "detail": f"{ticket} has an unfinished run"})
        try:
            path = worktrees.remove(session.ws, ticket, repo, session.deps.vcs_runner if session._deps else None)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(409, {"error": "refused", "detail": str(e)}) from e
        return {"removed": path}

    # ------------------------------------------------------------------ live events (SSE)
    @api.get("/api/stream")
    async def stream(request: Request, after: int = -1, run_id: str | None = None):
        session, _ = state.need()
        last = session.store.last_seq() if after < 0 else after

        async def gen():
            nonlocal last
            yield f"event: hello\ndata: {json.dumps({'seq': last})}\n\n"
            idle = 0.0
            while not await request.is_disconnected():
                evs = await asyncio.to_thread(session.store.events, run_id, last, 500)
                for e in evs:
                    last = e["seq"]
                    yield f"id: {e['seq']}\nevent: devflow\ndata: {json.dumps(_slim(e), default=str)}\n\n"
                if evs:
                    idle = 0.0
                    continue
                await asyncio.sleep(0.4)
                idle += 0.4
                if idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ the app itself
    @api.get("/{path:path}")
    def spa(path: str):
        f = (STATIC / path).resolve()
        if path and f.is_file() and STATIC.resolve() in f.parents:
            return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable"} if "/assets/" in f"/{path}" else None)
        index = STATIC / "index.html"
        if not index.exists():
            return HTMLResponse("<p>The UI is not built. Run <code>npm --prefix frontend run build</code>.</p>", status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return api


# ---------------------------------------------------------------------- helpers
def _deps_or_none(session: Session):
    try:
        return session.deps
    except Exception:  # noqa: BLE001
        return None


def _run_or_404(session: Session, run_id: str) -> dict:
    r = session.store.run(run_id)
    if not r:
        raise HTTPException(404, {"error": "not_found", "detail": f"no run {run_id}"})
    return r


def graph_json(g) -> dict:
    dg = g.get_graph()
    nodes = [{"id": n.id} for n in dg.nodes.values()]
    edges = [{"source": e.source, "target": e.target, "conditional": bool(e.conditional)} for e in dg.edges]
    return {"nodes": nodes, "edges": edges}


def form_from_schema(schema: dict) -> list[dict]:
    """A start form for a graph without a custom one: required fields, enums and lists become inputs."""
    req = set(schema.get("required") or [])
    fields = []
    for name, prop in (schema.get("properties") or {}).items():
        t = prop.get("type")
        anyof = [a.get("type") for a in prop.get("anyOf", []) if a.get("type") != "null"]
        t = t or (anyof[0] if anyof else "string")
        f = {"name": name, "label": prop.get("title") or name.replace("_", " ").capitalize(), "required": name in req,
             "help": prop.get("description", "")}
        if "enum" in prop:
            f.update(type="select", options=prop["enum"])
        elif t == "boolean":
            f.update(type="bool")
        elif t in ("integer", "number"):
            f.update(type="number")
        elif t == "array":
            f.update(type="list")
        elif t == "object":
            f.update(type="json")
        else:
            f.update(type="text")
        fields.append(f)
    return fields


def _live(session: Session, r: dict, full: bool = False) -> dict:
    """What the lists and the detail need from the graph state of an unfinished (or, with full, any) run."""
    try:
        snap = session.graph(r["run_id"]).get_state(session.cfg(r["run_id"]))
    except Exception as e:  # noqa: BLE001
        return {"state_error": f"{type(e).__name__}: {e}"}
    v = snap.values or {}
    pc = session._pending(snap) if r["status"] == "WAITING_HUMAN" else {}
    repos = {n: {k: rs.get(k) for k in ("status", "fix_attempts_used", "branch", "implemented", "pushed_sha")}
             for n, rs in (v.get("repos") or {}).items()} if isinstance(v.get("repos"), dict) else {}
    waves = ((v.get("dag") or {}).get("waves") or []) if isinstance(v.get("dag"), dict) else []
    for i, wave in enumerate(waves):
        for n in wave:
            if n in repos:
                repos[n]["wave"] = i
    out = {"pending": jsonable(pc) if pc else None, "repos": repos, "next": list(snap.next),
           "waiting_since": r["updated_at"] if r["status"] == "WAITING_HUMAN" else None}
    ticket = v.get("ticket") if isinstance(v.get("ticket"), dict) else None
    if ticket:
        out["ticket_info"] = {"key": ticket.get("key"), "title": ticket.get("title"), "url": ticket.get("url", "")}
    if full:
        mrs = v.get("mrs") or {}
        out.update({
            "scope": v.get("scope") or {}, "dag": jsonable(v.get("dag")) if v.get("dag") else None,
            "decisions": jsonable(v.get("decisions") or []), "mrs": jsonable(mrs) if isinstance(mrs, dict) else {},
            "warnings": v.get("env_warnings") or [], "output": str(v.get("output") or ""),
            "repo_state": trim({n: rs for n, rs in (v.get("repos") or {}).items()}) if isinstance(v.get("repos"), dict) else {},
            "scope_gaps": jsonable(v.get("scope_gaps") or []),
            "ticket_description": (ticket or {}).get("description", "")[:2000] if ticket else "",
        })
    return out


def _node_input(session: Session, run_id: str, task_id: str | None):
    if not task_id:
        return None
    try:
        for snap in session.graph(run_id).get_state_history(session.cfg(run_id)):
            for t in snap.tasks:
                if t.id == task_id:
                    return trim(snap.values, 30000)
    except Exception:  # noqa: BLE001
        return None
    return None


def _usage_totals(session: Session, run_id: str | None = None) -> dict:
    out: dict = {}
    for u in session.store.usage(run_id):
        u = _with_cost(session, u)
        t = out.setdefault(u["run_id"], {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost_usd": 0.0, "priced": True})
        t["input"] += u["input_tokens"]
        t["output"] += u["output_tokens"]
        t["cache_write"] += u["cache_write_tokens"]
        t["cache_read"] += u["cache_read_tokens"]
        if u["cost_usd"] is None:
            t["priced"] = False
        else:
            t["cost_usd"] += u["cost_usd"]
    return out


def _with_cost(session: Session, u: dict) -> dict:
    """Agent SDK runs report their own cost; API calls are priced from `prices` in workspace.yaml (USD per MTok)."""
    u = dict(u)
    if u.get("cost_usd") is not None:
        return u
    prices = session.ws.prices or {}
    model = u.get("model") or ""
    p = prices.get(model) or next((v for k, v in prices.items() if model.startswith(k)), None)
    if p:
        u["cost_usd"] = (u["input_tokens"] * p.get("input", 0) + u["output_tokens"] * p.get("output", 0)
                         + u["cache_write_tokens"] * p.get("cache_write", p.get("input", 0) * 1.25)
                         + u["cache_read_tokens"] * p.get("cache_read", p.get("input", 0) * 0.1)) / 1e6
    return u


def _sum(rows: list[dict]) -> dict:
    t = {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0, "calls": len(rows),
         "unpriced_calls": 0, "duration_s": 0.0}
    for u in rows:
        for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
            t[k] += u[k] or 0
        t["duration_s"] += u.get("duration_s") or 0
        if u["cost_usd"] is None:
            t["unpriced_calls"] += 1
        else:
            t["cost_usd"] += u["cost_usd"]
    total_in = t["input_tokens"] + t["cache_read_tokens"] + t["cache_write_tokens"]
    t["cache_hit_rate"] = (t["cache_read_tokens"] / total_in) if total_in else 0.0
    return t


def _group(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for u in rows:
        groups.setdefault(u.get(key) or "", []).append(u)
    return [{key: k, "key": k, **_sum(v)} for k, v in groups.items()]


def _slim(e: dict) -> dict:
    """SSE carries small events; big node results are fetched on demand."""
    if e["kind"] == "node_finished" and e.get("data"):
        d = dict(e["data"])
        d.pop("result", None)
        return {**e, "data": d}
    return e
