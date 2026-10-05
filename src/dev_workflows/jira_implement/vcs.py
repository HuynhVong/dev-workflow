"""The ONLY place git/glab run. Every call is prefixed with `rtk` and checked against scope and a deny-list."""
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .scope import ScopeGuard


class VcsPolicyError(PermissionError):
    pass


class VcsError(RuntimeError):
    def __init__(self, cmd: list[str], result: subprocess.CompletedProcess):
        self.cmd, self.result = cmd, result
        super().__init__(f"{shlex.join(cmd)} failed ({result.returncode}): {(result.stderr or result.stdout).strip()[:500]}")


Runner = Callable[[list[str], str], subprocess.CompletedProcess]
# Worktrees of one repo share its config and refs, so parallel runs can briefly collide on git's lock files.
LOCK_CONTENTION = re.compile(r"could not lock config file|Unable to create '[^']*\.lock'|cannot lock ref|unable to lock", re.I)


def default_runner(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)


def check_policy(tool: str, args: Sequence[str]) -> None:
    """Refuse anything that could rewrite history, merge, approve, delete branches or touch CI/CD."""
    if tool not in ("git", "glab"):
        raise VcsPolicyError(f"run_vcs only runs git or glab, not {tool!r}")
    a = list(args)
    joined = " ".join(a)
    if tool == "git":
        if a[:1] == ["push"] and any(x in ("-f", "--force", "--force-with-lease", "--delete", "-d", "--mirror") or x.startswith("--force") or x.startswith("+") for x in a[1:]):
            raise VcsPolicyError(f"forbidden: git {joined}")
        if a[:1] == ["branch"] and any(x in ("-d", "-D", "--delete", "-m", "-M", "--move", "-f", "--force") for x in a[1:]):
            raise VcsPolicyError(f"forbidden: git {joined}")
        if a[:1] == ["reset"] and "--hard" in a:
            raise VcsPolicyError(f"forbidden: git {joined}")
        if a[:1] in (["rebase"], ["filter-branch"], ["clean"]):
            raise VcsPolicyError(f"forbidden: git {joined}")
        if a[:1] == ["worktree"] and (len(a) < 2 or a[1] not in ("add", "list", "remove")
                                      or (a[1] == "remove" and any(x in ("-f", "--force") for x in a[2:]))):
            raise VcsPolicyError(f"forbidden: git {joined} (worktrees are only added, listed, or removed when clean)")
    else:
        if a[:1] in (["ci"], ["pipeline"], ["job"], ["schedule"], ["variable"]):
            raise VcsPolicyError(f"forbidden: this workflow does not touch CI/CD (glab {joined})")
        if a[:1] == ["mr"] and len(a) > 1 and a[1] in ("merge", "approve", "revoke", "close", "delete", "rebase", "ready"):
            raise VcsPolicyError(f"forbidden: glab {joined}")
        if a[:1] == ["mr"] and len(a) > 1 and a[1] == "update" and any(x in ("--ready", "-r", "--draft=false") for x in a[2:]):
            raise VcsPolicyError("forbidden: MRs stay draft")
        if a[:1] == ["api"]:
            _check_api(a[1:])


MR_DISCUSSIONS = re.compile(r"^projects/[^/\s]+/merge_requests/\d+/discussions(?P<notes>/[0-9a-f]+/notes(?P<note>/\d+)?)?(?:\?[\w=&]*)?$")


def _check_api(a: list[str]) -> None:
    """`glab api` is refused except for the MR discussion calls address-review needs: list discussions (GET),
    reply in a thread (POST .../notes) and edit that reply (PUT .../notes/<id>), with only a `body` field.
    Resolving threads, pipelines and every other endpoint stay forbidden."""
    method, path, fields, i = "GET", "", [], 0
    while i < len(a):
        x = a[i]
        if x in ("-X", "--method") and i + 1 < len(a):
            method, i = a[i + 1].upper(), i + 2
        elif x in ("-f", "--raw-field") and i + 1 < len(a):
            fields.append(a[i + 1])
            i += 2
        elif not x.startswith("-") and not path:
            path, i = x, i + 1
        else:
            raise VcsPolicyError(f"forbidden: glab api option {x!r}")
    m = MR_DISCUSSIONS.match(path)
    allowed = m and (
        (method == "GET" and not m["notes"] and not fields)
        or (method == "POST" and m["notes"] and not m["note"])
        or (method == "PUT" and m["note"]))
    if not allowed or any(not f.startswith("body=") for f in fields) or (method != "GET" and len(fields) != 1):
        raise VcsPolicyError(f"forbidden: glab api {method} {path} (only MR discussion read/reply/edit is allowed)")


@dataclass
class Vcs:
    scope: ScopeGuard
    prefix: str = "rtk"
    runner: Runner = default_runner

    def run(self, repo: str, tool: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """run_vcs(): `rtk <git|glab> args...` inside an in-scope repo."""
        cwd = self.scope.require_repo(repo)
        check_policy(tool, args)
        cmd = [*shlex.split(self.prefix), tool, *args] if self.prefix else [tool, *args]
        result = self.runner(cmd, cwd)
        for attempt in range(1, 8):  # another run held a shared git lock for a moment: wait and retry
            if tool != "git" or result.returncode == 0 or not LOCK_CONTENTION.search(result.stderr or ""):
                break
            time.sleep(0.1 * attempt)
            result = self.runner(cmd, cwd)
        if check and result.returncode != 0:
            raise VcsError(cmd, result)
        return result

    def git(self, repo: str, *args: str, check: bool = True) -> str:
        return self.run(repo, "git", *args, check=check).stdout.strip()

    def glab(self, repo: str, *args: str, check: bool = True) -> str:
        return self.run(repo, "glab", *args, check=check).stdout.strip()

    # --- read helpers -------------------------------------------------
    def ok(self, repo: str, *args: str) -> bool:
        return self.run(repo, "git", *args, check=False).returncode == 0

    def local_branch_exists(self, repo: str, branch: str) -> bool:
        return self.ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")

    def remote_branch_exists(self, repo: str, branch: str) -> bool:
        return bool(self.git(repo, "ls-remote", "--heads", "origin", branch, check=False))

    def config_get(self, repo: str, key: str) -> str:
        return self.git(repo, "config", "--get", key, check=False)

    def is_dirty(self, repo: str) -> bool:
        return bool(self.git(repo, "status", "--porcelain"))

    def head(self, repo: str, ref: str = "HEAD") -> str:
        return self.git(repo, "rev-parse", ref)

    def ahead_behind(self, repo: str, left: str, right: str) -> tuple[int, int]:
        out = self.git(repo, "rev-list", "--left-right", "--count", f"{left}...{right}")
        a, b = re.split(r"\s+", out.strip())[:2]
        return int(a), int(b)

    def changed_files(self, repo: str) -> list[str]:
        return [l[3:] for l in self.git(repo, "status", "--porcelain").splitlines() if l.strip()]

    def worktrees(self, repo: str) -> list[dict]:
        """`git worktree list --porcelain` as [{path, head, branch, detached}] (the main clone included)."""
        out, cur = [], {}
        for line in self.git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
            if not line.strip():
                if cur:
                    out.append(cur)
                cur = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                cur = {"path": value, "head": "", "branch": "", "detached": False}
            elif key == "HEAD":
                cur["head"] = value
            elif key == "branch":
                cur["branch"] = value.removeprefix("refs/heads/")
            elif key == "detached":
                cur["detached"] = True
        return out

    # --- ticket review: read-only checks of the developer's own checkout ------------------------------------
    def commit_of(self, repo: str, sha: str) -> str:
        """The full SHA if `sha` names a commit this clone has, else ""."""
        r = self.run(repo, "git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
        return (r.stdout or "").strip() if r.returncode == 0 else ""

    def in_head(self, repo: str, sha: str) -> bool:
        return self.ok(repo, "merge-base", "--is-ancestor", sha, "HEAD")

    def branches_containing(self, repo: str, sha: str) -> list[str]:
        """Remote branches (as of the last fetch) that contain the commit, e.g. ["origin/AQS-5512"]."""
        out = self.git(repo, "branch", "-r", "--contains", sha, check=False)
        return [b.strip() for b in out.splitlines() if b.strip() and "->" not in b]

    def current_branch(self, repo: str) -> str:
        return self.git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)

    def upstream(self, repo: str) -> str:
        return self.git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)

    def show_commit(self, repo: str, sha: str) -> str:
        """One commit's message and patch."""
        return self.git(repo, "show", "--format=commit %H%nAuthor: %an%n%n%B", sha)

    def diff_against(self, repo: str, base: str) -> str:
        """Committed + uncommitted changes relative to the base branch (merge-base)."""
        mb = self.git(repo, "merge-base", base, "HEAD")
        self.git(repo, "add", "-N", ".")  # include new files in the diff without staging content
        return self.git(repo, "diff", mb)
