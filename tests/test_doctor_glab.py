import subprocess
from types import SimpleNamespace

import pytest

from dev_workflows.doctor import glab_auth_checks, origin_host


@pytest.mark.parametrize("url, host", [
    ("https://gitlab.acme.io/team/api.git", "gitlab.acme.io"),
    ("https://oauth2:tok@gitlab.acme.io:8443/team/api.git", "gitlab.acme.io"),
    ("ssh://git@gitlab.acme.io:2222/team/api.git", "gitlab.acme.io"),
    ("git@gitlab.acme.io:team/api.git", "gitlab.acme.io"),
    ("git@gitlab.com:me/x.git\n", "gitlab.com"),
    ("", ""),
    ("/srv/git/local.git", ""),
])
def test_origin_host(url, host):
    assert origin_host(url) == host


class FakeVcs:
    def __init__(self, origins: dict[str, str], logged_in: set[str]):
        self.origins, self.logged_in, self.calls = origins, logged_in, []

    def run(self, repo, tool, *args, check=True):
        self.calls.append((repo, tool, *args))
        if tool == "git":
            return subprocess.CompletedProcess(args, 0, stdout=self.origins[repo] + "\n", stderr="")
        host = args[args.index("--hostname") + 1]
        ok = host in self.logged_in
        return subprocess.CompletedProcess(args, 0 if ok else 1, stdout="", stderr=f"{host}: " + ("Logged in" if ok else "401 Unauthorized"))


def workspace(tmp_path, names):
    repos = {}
    for n in names:
        (tmp_path / n / ".git").mkdir(parents=True)
        repos[n] = SimpleNamespace(path=str(tmp_path / n))
    return SimpleNamespace(repos=repos, vcs_prefix="rtk")


def test_only_the_repos_hosts_are_checked(tmp_path):
    ws = workspace(tmp_path, ["api", "web"])
    vcs = FakeVcs({"api": "git@gitlab.acme.io:team/api.git", "web": "https://gitlab.acme.io/team/web.git"}, {"gitlab.acme.io"})
    [c] = glab_auth_checks(ws, vcs, ["api", "web"], "api")
    assert c.id == "cli.glab" and c.status == "ok" and "--hostname gitlab.acme.io" in c.label
    assert [x for x in vcs.calls if x[1] == "glab"] == [("api", "glab", "auth", "status", "--hostname", "gitlab.acme.io")]


def test_each_host_gets_its_own_check_and_fix(tmp_path):
    ws = workspace(tmp_path, ["api", "lib"])
    vcs = FakeVcs({"api": "git@gitlab.acme.io:team/api.git", "lib": "https://gitlab.com/oss/lib.git"}, {"gitlab.acme.io"})
    checks = {c.id: c for c in glab_auth_checks(ws, vcs, ["api", "lib"], "api")}
    assert checks["cli.glab.gitlab.acme.io"].status == "ok"
    bad = checks["cli.glab.gitlab.com"]
    assert bad.status == "fail" and bad.blocking and "glab auth login --hostname gitlab.com" in bad.fix


def test_no_origin_is_a_non_blocking_skip(tmp_path):
    ws = workspace(tmp_path, ["api"])
    [c] = glab_auth_checks(ws, FakeVcs({"api": ""}, set()), ["api"], "api")
    assert c.status == "fail" and not c.blocking
