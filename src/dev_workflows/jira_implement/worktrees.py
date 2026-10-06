"""One git worktree per run per repo, so several tickets can be worked on at once.

`repos[].path` in workspace.yaml is the developer's normal clone. devflow never edits or switches it: each ticket
works in `<worktree_root>/<TICKET>/<repo>`, added from that clone with `rtk git worktree add`. The worktree starts
detached at a freshly fetched `origin/<base>` (so discovery can read it before any branch exists); `prepare_branches`
then cuts the ticket branch inside it. A worktree is never removed automatically while it holds work, and its branch is
never deleted: an aborted run only drops its *clean* worktrees (`release`), and preflight only prunes stale git
bookkeeping and removes an empty leftover folder.
"""
import os
import re
import shutil
from pathlib import Path

from .ledger import Effect, Store, perform
from .scope import ScopeGuard
from .vcs import Vcs
from .workspace import Workspace


def source_vcs(ws: Workspace, repos: list[str], runner=None) -> Vcs:
    """A Vcs over the main clones. Used only to fetch and to add or list worktrees."""
    kw = {"runner": runner} if runner else {}
    return Vcs(ScopeGuard.create({r: ws.repos[r].path for r in repos}), prefix=ws.vcs_prefix, **kw)


def _same(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def gaps(src: Vcs, ws: Workspace, ticket: str, repos: list[str]) -> list[str]:
    """Preflight: the worktree path is free or already this ticket's worktree, and the ticket branch is not checked
    out anywhere else (git allows a branch in only one worktree)."""
    out = []
    for repo in repos:
        path = ws.worktree(ticket, repo)
        try:
            src.git(repo, "worktree", "prune")  # forget worktrees whose folder is gone
            listed = src.worktrees(repo)
        except Exception as e:  # noqa: BLE001
            out.append(f"{repo}: cannot list worktrees: {e}")
            continue
        mine = any(_same(w["path"], path) for w in listed)
        if Path(path).is_dir() and not mine and not any(Path(path).iterdir()):
            Path(path).rmdir()  # an empty leftover folder holds nothing: let `worktree add` use the path
        if Path(path).exists() and not mine:
            out.append(f"{repo}: {path} exists but is not a worktree of {repo}; move it away first")
        for w in listed:
            if w["branch"] == ticket and not _same(w["path"], path):
                out.append(f"{repo}: branch {ticket} is checked out in {w['path']}; switch that checkout to another "
                           f"branch first (devflow works on {ticket} in {path})")
    return out


def ensure(src: Vcs, ws: Workspace, store: Store, run_id: str, ticket: str, repo: str, run_cmd) -> str:
    """Create (or reuse) the run's worktree for one repo and run its one-time setup. Returns the worktree path."""
    path = ws.worktree(ticket, repo)
    cfg = ws.repo(repo)
    if not any(_same(w["path"], path) for w in src.worktrees(repo)):
        src.git(repo, "fetch", "origin")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if src.local_branch_exists(repo, ticket):
            src.git(repo, "worktree", "add", path, ticket)  # a resumed ticket or address-review: keep its branch
        else:
            src.git(repo, "worktree", "add", "--detach", path, f"origin/{cfg.base_branch}")
        store.audit(run_id, "worktree_added", {"repo": repo, "path": path})
    eff = Effect(run_id, "worktree_setup", repo, f"setup:{path}")

    def done():
        st = store.effect_status(eff.id)
        return st[1] if st and st[0] == "done" else None

    def setup():
        copied = []
        for rel in cfg.copy_files:
            src_file, dst = Path(cfg.path, rel), Path(path, rel)
            if src_file.is_file() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)
                copied.append(rel)
        ran = []
        for cmd in cfg.setup_commands:
            r = run_cmd(cmd, path)
            ran.append({"command": cmd, "ok": r.returncode == 0})
            if r.returncode != 0:
                raise RuntimeError(f"{repo}: setup command `{cmd}` failed in {path}: {((r.stderr or '') + (r.stdout or '')).strip()[-500:]}")
        return {"copied": copied, "commands": ran}

    if cfg.copy_files or cfg.setup_commands:
        perform(store, eff, done, setup)
    return path


def env_for(scope: dict[str, str]) -> dict[str, str]:
    """DEVFLOW_WORKTREE_<REPO> for every repo of the run, so a check can build against its sibling worktrees."""
    return {"DEVFLOW_WORKTREE_" + re.sub(r"[^A-Za-z0-9]", "_", r).upper(): p for r, p in scope.items()}


def list_all(ws: Workspace, runner=None) -> list[dict]:
    """Every devflow worktree on disk: ticket, repo, branch, uncommitted changes, size (for the Worktrees page)."""
    root = Path(ws.worktree_root)
    out = []
    if not root.is_dir():
        return out
    for ticket_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for repo_dir in sorted(p for p in ticket_dir.iterdir() if p.is_dir()):
            repo = repo_dir.name
            item = {"ticket": ticket_dir.name, "repo": repo, "path": str(repo_dir), "branch": "", "dirty": None,
                    "size_bytes": _size(repo_dir), "known_repo": repo in ws.repos}
            if repo in ws.repos:
                try:
                    v = Vcs(ScopeGuard.create({repo: str(repo_dir)}), prefix=ws.vcs_prefix, **({"runner": runner} if runner else {}))
                    item["branch"] = v.git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
                    item["dirty"] = v.is_dirty(repo)
                except Exception:  # noqa: BLE001
                    pass
            out.append(item)
    return out


def remove(ws: Workspace, ticket: str, repo: str, runner=None) -> str:
    """Remove one clean worktree (never forced; the branch stays). Raises when there are uncommitted changes."""
    path = ws.worktree(ticket, repo)
    wt = Vcs(ScopeGuard.create({repo: path}), prefix=ws.vcs_prefix, **({"runner": runner} if runner else {}))
    if wt.is_dirty(repo):
        raise RuntimeError(f"{path} has uncommitted changes; commit or discard them by hand first")
    source_vcs(ws, [repo], runner).git(repo, "worktree", "remove", path)
    parent = Path(path).parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    return path


def release(ws: Workspace, ticket: str, runner=None) -> dict[str, list[str]]:
    """Called when a run is aborted: remove this ticket's worktrees that have no uncommitted changes (never forced, the
    branches stay). Dirty ones, and anything that cannot be removed, are kept. Returns {"removed": [...], "kept": [...]}."""
    out: dict[str, list[str]] = {"removed": [], "kept": []}
    for w in list_all(ws, runner):
        if w["ticket"] != ticket:
            continue
        if w["dirty"] is False:
            try:
                out["removed"].append(remove(ws, ticket, w["repo"], runner))
                continue
            except Exception:  # noqa: BLE001
                pass
        out["kept"].append(w["path"])
    return out


def _size(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        if ".git" in dirnames and dirpath == str(path):
            dirnames.remove(".git")
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total
