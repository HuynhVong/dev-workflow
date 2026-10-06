"""Offline harness: real git repos with a bare 'origin', fake `rtk` and `glab` on PATH, fake MCP and coding agent."""
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

from dev_workflows.jira_implement.coding_agent import CodingResult
from dev_workflows.jira_implement.confluence import ConfluenceReader
from dev_workflows.jira_implement.graph import Deps
from dev_workflows.jira_implement.jira import JiraGateway
from dev_workflows.jira_implement.ledger import Store
from dev_workflows.jira_implement.models import (Analysis, ContractCheck, ContractReview, DagEdge, FeedbackAnalysis, Impact,
                                                 Outdated, Plan, RepoTasks, RequirementContext, Risk, TestPlanItem)
from dev_workflows.jira_implement.workspace import load_workspace
from dev_workflows.routing import Routing, SkillRegistry
from dev_workflows.workflows.pr_review import LensReview, Triage, Verdict

FAKE_GLAB = r'''#!/usr/bin/env python3
import fcntl, json, os, sys
db = os.environ["FAKE_GLAB_DB"]
_lock = open(db + ".lock", "w"); fcntl.flock(_lock, fcntl.LOCK_EX)  # parallel runs share this fake GitLab
data = json.load(open(db)) if os.path.exists(db) else {}
repo = os.path.basename(os.getcwd())
a = sys.argv[1:]
log = open(db + ".log", "a"); log.write(repo + " " + " ".join(a) + "\n"); log.close()
if a[:2] == ["auth", "status"]:
    sys.exit(0)
if a[:2] == ["mr", "list"]:
    if "--source-branch" in a:
        br = a[a.index("--source-branch") + 1]
        print(json.dumps([m for m in data.get(repo, []) if m["source"] == br]))
    sys.exit(0)
if a[:2] == ["mr", "create"]:
    br = a[a.index("--source-branch") + 1]
    mrs = data.setdefault(repo, [])
    mrs.append({"iid": len(mrs) + 1, "source": br, "web_url": f"https://gitlab/{repo}/-/merge_requests/{len(mrs) + 1}",
                "draft": "--draft" in a, "description": a[a.index("--description") + 1]})
    json.dump(data, open(db, "w"))
    sys.exit(0)
if a[:2] == ["mr", "update"]:
    for m in data.get(repo, []):
        if str(m["iid"]) == a[2]:
            m["description"] = a[a.index("--description") + 1]
    json.dump(data, open(db, "w"))
    sys.exit(0)
if a[:1] == ["api"]:
    import re
    method, fields, path, i = "GET", [], None, 1
    while i < len(a):
        if a[i] in ("--method", "-X"):
            method, i = a[i + 1], i + 2
        elif a[i] in ("-f", "--raw-field"):
            fields.append(a[i + 1]); i += 2
        else:
            path, i = a[i], i + 1
    m = re.match(r"projects/[^/]+/merge_requests/(\d+)/discussions(?:/([0-9a-f]+)/notes(?:/(\d+))?)?", path.split("?")[0])
    ds = data.setdefault("discussions", {}).setdefault(repo, {}).setdefault(m[1], [])
    body = dict(f.split("=", 1) for f in fields).get("body")
    if method == "GET":
        print(json.dumps(ds)); sys.exit(0)
    d = next(d for d in ds if d["id"] == m[2])
    if method == "POST":
        note = {"id": 1000 + sum(len(x["notes"]) for x in ds), "body": body, "author": {"username": "dev"}, "resolvable": True, "resolved": False}
        d["notes"].append(note)
    else:
        note = next(n for n in d["notes"] if str(n["id"]) == m[3]); note["body"] = body
    json.dump(data, open(db, "w")); print(json.dumps(note)); sys.exit(0)
print("unsupported fake glab call", a, file=sys.stderr)
sys.exit(2)
'''


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeMcp:
    def __init__(self, tools, status="In Progress", refuse=()):
        self.tools = set(tools)
        self.calls = []
        self.status = status
        self.comments = []
        self.attachments = []
        self.refuse = set(refuse)  # tools that answer like Jira without permission

    def list_tools(self):
        return sorted(self.tools)

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.refuse:
            raise RuntimeError(f"atlassian.{tool} failed: Error 403: Forbidden - you do not have permission")
        if tool == "jira_get_issue":
            return {"key": args["issue_key"], "fields": {
                "summary": "Export orders as CSV", "description": "Export with filters. Spec: https://acme.atlassian.net/wiki/spaces/SHOP/pages/12345/Export",
                "status": {"name": self.status}, "assignee": {"emailAddress": "dev@acme.io"},
                "comment": {"comments": list(self.comments)}, "attachment": [{"filename": n} for n in self.attachments]}}
        if tool == "jira_download_attachments":
            return {"downloaded": []}
        if tool == "jira_update_issue":
            self.attachments += [Path(p).name for p in json.loads(args["attachments"])]
            return {"ok": True}
        if tool == "jira_get_transitions":
            return [{"id": "31", "to": {"name": "Code Review"}}]
        if tool == "jira_transition_issue":
            self.status = "Code Review"
            return {"ok": True}
        if tool == "jira_add_comment":
            self.comments.append({"id": str(len(self.comments) + 1), "body": args["comment"]})
            return {"ok": True}
        if tool == "jira_edit_comment":
            for c in self.comments:
                if c["id"] == args["comment_id"]:
                    c["body"] = args["comment"]
            return {"ok": True}
        if tool.startswith("confluence_"):
            return {"title": "Export spec", "body": "Rules: max 50k rows"}
        raise AssertionError(f"unexpected tool {tool}")

    def writes(self):
        return [c for c in self.calls if c[0] in ("jira_transition_issue", "jira_add_comment", "jira_edit_comment", "jira_update_issue")]


class FakeCoder:
    def __init__(self, fail_repos=(), needs=None):
        self.calls, self.steps = [], []
        self.fail_repos = set(fail_repos)
        self.needs = needs or {}

    def implement(self, repo, path, instructions, step="implement", escalate=False):
        self.calls.append(("implement", repo, instructions))
        self.steps.append((step, repo, escalate))
        Path(path, "feature.txt").write_text(Path(path, "feature.txt").read_text() + "x\n" if Path(path, "feature.txt").exists() else "x\n")
        return CodingResult(ok=True, summary=f"implemented {repo}", files_changed=["feature.txt"],
                            out_of_scope_needs=self.needs.pop(repo, []))

    def explore(self, repo, path, question, step="discover_repos"):
        self.calls.append(("explore", repo, question))
        return CodingResult(ok=True, summary="relevant", files_changed=["src/x"], findings={})

    def verify(self, repos, instructions, step="integration_check"):
        self.calls.append(("verify", tuple(repos), instructions))
        return CodingResult(ok=True, findings={"passed": True, "results": []})


class FakeLLM:
    """Answers by schema; `overrides` maps schema -> list of answers (consumed in order, last one repeats)."""

    def __init__(self, repos, edges, integration=False, e2e=False, overrides=None, out_of_scope=None):
        self.calls, self.steps, self.images = [], [], []
        self.answers = {
            RequirementContext: [RequirementContext(requirement=[], current_business=[], conflicts=[], pages=[])],
            Analysis: [Analysis(summary="Export orders", kind="feature", acceptance_criteria=["CSV respects filters"], questions=[])],
            Impact: [Impact(layers=["backend", "frontend"], candidate_repos=list(repos), out_of_scope_repos=out_of_scope or [],
                            contract_changes=["GET /orders/export"], migrations=[], risk=Risk(level="medium", reasons=["new API"]),
                            integration_required=integration, e2e_required=e2e, manual_test_focus=["large export"], questions=[])],
            Plan: [Plan(repos=[RepoTasks(repo=r, tasks=[f"do {r}"], likely_files=[]) for r in repos],
                        edges=[DagEdge(upstream=u, downstream=d, carries="contract") for u, d in edges],
                        contracts=["GET /orders/export -> text/csv"], migrations=[],
                        test_plan=[TestPlanItem(criterion="CSV respects filters", tests=["test_export"])], risks=[])],
            ContractCheck: [ContractCheck(ok=True, issues=[])],
            ContractReview: [ContractReview(blockers=[], notes=[])],
            FeedbackAnalysis: [FeedbackAnalysis(items=[])],
            Outdated: [Outdated(pages=[])],
            Triage: [Triage(summary="s", touches_frontend=False, risk="low", lenses=[])],
            LensReview: [LensReview(findings=[])],
            Verdict: [Verdict(decision="approve", summary="ok")],
        }
        for k, v in (overrides or {}).items():
            self.answers[k] = list(v)

    def structured(self, system, prompt, schema, images=(), step=""):
        self.calls.append((schema, prompt))
        self.steps.append(step)
        self.images.append(list(images))
        q = self.answers[schema]
        return q.pop(0) if len(q) > 1 else q[0]


def make_env(tmp_path: Path, repos=("api", "web"), checks=None, extra_repos=()):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"rtk": '#!/bin/sh\nexec "$@"\n', "glab": FAKE_GLAB}.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    ws_repos = {}
    for r in list(repos) + list(extra_repos):
        origin = tmp_path / "origin" / f"{r}.git"
        sh("git", "init", "-q", "--bare", "-b", "develop", str(origin))
        work = tmp_path / "ws" / r
        sh("git", "clone", "-q", str(origin), str(work))
        sh("git", "-C", str(work), "checkout", "-q", "-b", "develop")
        sh("git", "-C", str(work), "config", "user.email", "dev@acme.io")
        sh("git", "-C", str(work), "config", "user.name", "Dev")
        (work / "README.md").write_text(r)
        sh("git", "-C", str(work), "add", ".")
        sh("git", "-C", str(work), "commit", "-qm", "init")
        sh("git", "-C", str(work), "push", "-q", "-u", "origin", "develop")
        ws_repos[r] = {"path": str(work), "gitlab_project": f"acme/{r}", "has_ui": False,
                       "commands": {"lint": (checks or {}).get(r, "test -f feature.txt")}}
    (tmp_path / "workspace.yaml").write_text(json.dumps({"repos": ws_repos, "state_dir": str(tmp_path / ".devflow"),
                                                         "worktree_root": str(tmp_path / "wt")}))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GLAB_DB": str(tmp_path / "glab.json")}
    return load_workspace(tmp_path / "workspace.yaml"), env


def no_skills():
    return Routing(registry=SkillRegistry([]))


def make_deps(tmp_path, ws, env, llm, coder, mcp=None, routing=None):
    mcp = mcp or FakeMcp(list(ws.jira_tools.values()) + list(ws.confluence_tools.values()))
    store = Store(str(tmp_path / "runs.sqlite"))

    def runner(cmd, cwd):
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)

    def cmd_runner(cmd, cwd, extra=None):
        return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env={**env, **(extra or {})})

    deps = Deps(workspace=ws, store=store, llm=llm, jira=JiraGateway(mcp, ws.jira_tools, ws.status_order),
                confluence=ConfluenceReader(mcp, ws.confluence_tools), coder_factory=lambda scope: coder,
                vcs_runner=runner, cmd_runner=cmd_runner, which=lambda name: f"/usr/bin/{name}", routing=routing or no_skills())
    return deps, store, mcp


def seed_review(tmp_path, ws, key="AQS-1", threads=None):
    """Existing <KEY> branches pushed to origin, one draft MR per repo, and unresolved review threads."""
    db = glab_db(tmp_path)
    for r, cfg in ws.repos.items():
        sh("git", "-C", cfg.path, "checkout", "-q", "-b", key)
        Path(cfg.path, "feature.txt").write_text("v1\n")
        sh("git", "-C", cfg.path, "add", ".")
        sh("git", "-C", cfg.path, "commit", "-qm", f"{key}: first")
        sh("git", "-C", cfg.path, "push", "-q", "-u", "origin", key)
        sh("git", "-C", cfg.path, "checkout", "-q", "develop")  # the ticket branch lives in its worktree, not the clone
        db.setdefault(r, []).append({"iid": 1, "source": key, "web_url": f"https://gitlab/{r}/-/merge_requests/1", "draft": True,
                                     "title": f"Draft: {key}", "description": ""})
    for repo, tid, body in threads or []:
        db.setdefault("discussions", {}).setdefault(repo, {}).setdefault("1", []).append(
            {"id": tid, "notes": [{"id": len(db["discussions"][repo]["1"]) + 1, "body": body, "author": {"username": "reviewer"},
                                   "resolvable": True, "resolved": False, "position": {"new_path": "feature.txt", "new_line": 1}}]})
    (tmp_path / "glab.json").write_text(json.dumps(db))


def wt(ws, repo, key="AQS-1"):
    """The run's worktree for a repo (where every edit, branch and commit happens)."""
    return ws.worktree(key, repo)


def glab_db(tmp_path):
    p = tmp_path / "glab.json"
    return json.loads(p.read_text()) if p.exists() else {}
