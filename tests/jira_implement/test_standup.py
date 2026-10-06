import datetime as dt
import io
import json
from pathlib import Path

import pytest

from dev_workflows.jira_implement import standup
from dev_workflows.jira_implement.confluence import ConfluenceReader
from dev_workflows.jira_implement.graph import Deps
from dev_workflows.jira_implement.jira import JiraGateway
from dev_workflows.jira_implement.runner import Session
from dev_workflows.registry import Registry

from .harness import make_env, no_skills

ME = {"accountId": "acc-me", "displayName": "Huynh Vong", "emailAddress": "me@acme.io", "timeZone": "Asia/Ho_Chi_Minh"}
BOB = {"accountId": "acc-bob", "displayName": "Bob"}
STATUSES = dict(standup.DEFAULTS)


def move(at, frm, to, assignee=None, prev=None):
    """One history entry: a status change, optionally with an assignee change in the same entry."""
    items = [{"field": "status", "fromString": frm, "toString": to}]
    if assignee is not None:
        items.insert(0, {"field": "assignee", "from": (prev or {}).get("accountId"), "fromString": (prev or {}).get("displayName"),
                         "to": assignee["accountId"], "toString": assignee["displayName"]})
    return {"created": at, "items": items}


def assign(at, who, prev=None):
    return {"created": at, "items": [{"field": "assignee", "from": (prev or {}).get("accountId"), "fromString": (prev or {}).get("displayName"),
                                       "to": who["accountId"], "toString": who["displayName"]}]}


def comment(at, author, body):
    return {"created": at, "author": author, "body": body}


# Range 2026-10-01 .. 2026-10-02 in Asia/Ho_Chi_Minh (+07:00).
ISSUES = {
    "AQS-1": {"summary": "Export orders", "assignee": ME, "histories": [
        assign("2026-09-30T10:00:00.000+0700", ME), move("2026-10-01T09:00:00.000+0700", "Approved", "In Progress")]},
    "AQS-2": {"summary": "Someone else's start", "assignee": BOB, "histories": [
        assign("2026-09-30T10:00:00.000+0700", BOB), move("2026-10-01T09:00:00.000+0700", "Approved", "In Progress")]},
    "AQS-3": {"summary": "Started then handed over", "assignee": BOB, "histories": [
        assign("2026-09-30T10:00:00.000+0700", ME), move("2026-10-01T11:00:00.000+0700", "Approved", "In Progress"),
        assign("2026-10-02T15:00:00.000+0700", BOB, prev=ME)]},
    "AQS-4": {"summary": "Late evening start", "assignee": ME, "histories": [
        assign("2026-09-29T10:00:00.000+0700", ME), move("2026-10-02T16:30:00.000Z", "Approved", "In Progress")]},  # 23:30 local
    "AQS-5": {"summary": "Just after midnight", "assignee": ME, "histories": [
        assign("2026-09-29T10:00:00.000+0700", ME), move("2026-10-02T17:30:00.000Z", "Approved", "In Progress")]},  # 00:30 local, next day
    "AQS-10": {"summary": "Review assigned in the move", "assignee": ME, "histories": [
        assign("2026-09-20T10:00:00.000+0700", BOB), move("2026-09-25T10:00:00.000+0700", "Approved", "In Progress"),
        move("2026-10-02T10:00:00.000+0700", "In Progress", "In Review", assignee=ME, prev=BOB)],
        "comments": [comment("2026-10-02T14:00:00.000+0700", ME, "Checked the CSV filters, LGTM"),
                     comment("2026-10-02T15:00:00.000+0700", BOB, "thanks"),
                     comment("2026-09-28T09:00:00.000+0700", ME, "old question")]},
    "AQS-11": {"summary": "My own ticket going to review", "assignee": ME, "histories": [
        assign("2026-09-20T10:00:00.000+0700", ME), move("2026-09-22T10:00:00.000+0700", "Approved", "In Progress"),
        move("2026-10-01T10:00:00.000+0700", "In Progress", "In Review")]},
    "AQS-12": {"summary": "Review for Bob", "assignee": BOB, "histories": [
        assign("2026-09-20T10:00:00.000+0700", ME), move("2026-10-01T10:00:00.000+0700", "In Progress", "In Review", assignee=BOB, prev=ME)]},
    "AQS-13": {"summary": "Quiet review", "assignee": ME, "histories": [
        assign("2026-09-30T10:00:00.000+0700", ME), move("2026-10-01T16:00:00.000+0700", "In Progress", "In Review")]},
}


def last_status(issue):
    return next((it["toString"] for h in reversed(issue["histories"]) for it in h["items"] if it["field"] == "status"), "To Do")


class StandupMcp:
    """A Jira MCP with JQL search and issue history, recording every call (and refusing nothing: the standup never writes)."""

    def __init__(self, tools, issues=ISSUES, changelog_in_issue=True):
        self.tools, self.issues, self.calls, self.changelog_in_issue = set(tools), issues, [], changelog_in_issue

    def list_tools(self):
        return sorted(self.tools)

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "jira_get_user_profile":
            return dict(ME)
        if tool == "jira_search":
            if "currentUser() ORDER BY" in args["jql"]:
                return {"issues": [{"key": "AQS-1", "fields": {"assignee": ME}}], "total": 1}
            keys = sorted(self.issues)  # JQL is only a pre-filter: return every candidate, two per page
            page = keys[args["start_at"]:args["start_at"] + 2]
            return {"issues": [{"key": k} for k in page], "total": len(keys)} if args["limit"] >= 2 else {"issues": [{"key": keys[0]}]}
        if tool == "jira_get_issue":
            i = self.issues[args["issue_key"]]
            issue = {"key": args["issue_key"], "fields": {"summary": i["summary"], "description": "desc", "status": {"name": last_status(i)},
                                                          "assignee": i["assignee"], "comment": {"comments": i.get("comments", [])}}}
            if self.changelog_in_issue:
                issue["changelog"] = {"histories": i["histories"]}
            return issue
        if tool == "jira_batch_get_changelogs":
            key = args["issue_ids_or_keys"][0]
            simple = [{"created": h["created"], "items": [{"field": it["field"], "from_id": it.get("from"), "from_string": it.get("fromString"),
                                                           "to_id": it.get("to"), "to_string": it.get("toString")} for it in h["items"]]}
                      for h in self.issues[key]["histories"]]
            return [{"issue_id": key, "changelogs": simple}]
        raise AssertionError(f"unexpected tool {tool}")


class ReportLLM:
    def __init__(self):
        self.calls = []

    def structured(self, system, prompt, schema, images=(), step=""):
        self.calls.append((schema, prompt, step))
        if schema is standup.Digest:
            keys = [t["key"] for t in json.loads(prompt.split("<tickets>\n")[1].split("\n</tickets>")[0])]
            return standup.Digest(lines=[standup.TicketLine(key=k, what=f"what {k}", did=f"did {k}") for k in keys])
        n = sum(1 for c in self.calls if c[0] is standup.Report)
        return standup.Report(report=f"report v{n}\n" + prompt.split("<template>\n")[1].split("\n</template>")[0])


def make_session(tmp_path, mcp=None, llm=None, jira_user="me@acme.io"):
    ws, _ = make_env(tmp_path, repos=("web",))
    raw = json.loads((tmp_path / "workspace.yaml").read_text())
    raw["jira_user"] = jira_user
    raw["mcp_servers"] = {"atlassian": {"command": "mcp-atlassian", "env": {"JIRA_URL": "https://acme.atlassian.net/"}}}
    (tmp_path / "workspace.yaml").write_text(json.dumps(raw))
    from dev_workflows.jira_implement.workspace import load_workspace
    ws = load_workspace(tmp_path / "workspace.yaml")
    mcp = mcp or StandupMcp(list(ws.jira_tools.values()))
    llm = llm or ReportLLM()

    def factory(ws_, store):
        return Deps(workspace=ws_, store=store, llm=llm, jira=JiraGateway(mcp, ws_.jira_tools, ws_.status_order),
                    confluence=ConfluenceReader(mcp, ws_.confluence_tools), routing=no_skills())
    out = io.StringIO()
    return Session(ws, deps_factory=factory, ask=None, out=out, registry=Registry()), out, mcp, llm, ws


def bounds():
    return standup.day_range(dt.date(2026, 10, 1), dt.date(2026, 10, 2), standup.zone("Asia/Ho_Chi_Minh"))


def ticket(key):
    i = ISSUES[key]
    return {"histories": [{"created": h["created"], "items": [{"field": it["field"], "from": it.get("from"),
                                                               "from_string": it.get("fromString"), "to": it.get("to"),
                                                               "to_string": it.get("toString")} for it in h["items"]]}
                          for h in i["histories"]], "assignee": i["assignee"], "comments": i.get("comments", [])}


@pytest.mark.parametrize("key,group", [
    ("AQS-1", "started"),     # Approved -> In Progress in range, assigned to me
    ("AQS-2", None),          # same move, but Bob's ticket
    ("AQS-3", "started"),     # mine when it moved; reassigned to Bob afterwards still counts
    ("AQS-4", "started"),     # 23:30 local on the last day
    ("AQS-5", None),          # 00:30 local the day after the range
    ("AQS-10", "reviewed"),   # moved to In Review with me set as assignee in the same move
    ("AQS-11", None),         # my own ticket going to review is not a review I did
    ("AQS-12", None),         # moved to In Review but assigned to Bob
    ("AQS-13", "reviewed"),   # a review without any comment still counts
])
def test_classify_uses_the_history_and_the_assignee_at_that_moment(key, group):
    lo, hi = bounds()
    ids = standup.identities({"account_id": "acc-me", "display_name": "Huynh Vong", "email": "me@acme.io"})
    hit = standup.classify(ticket(key), ids, lo, hi, STATUSES)
    assert (hit or {}).get("group") == group


def test_my_comments_only_mine_and_only_in_range():
    lo, hi = bounds()
    ids = standup.identities({"account_id": "acc-me"})
    assert [c["body"] for c in standup.my_comments(ticket("AQS-10"), ids, lo, hi)] == ["Checked the CSV filters, LGTM"]


def test_dates_and_timestamps():
    assert standup.previous_working_day(dt.date(2026, 10, 5)) == dt.date(2026, 10, 2)  # Monday -> Friday
    assert standup.parse_ts("2026-10-01T09:00:00.000+0700") == standup.parse_ts("2026-10-01T02:00:00Z")
    assert standup.parse_ts("garbage") is None
    lo, hi = bounds()
    assert hi - lo == dt.timedelta(days=2)


def test_standup_end_to_end_read_only(tmp_path):
    s, out, mcp, llm, ws = make_session(tmp_path)
    run_id, status = s.start_run("standup", {"from_date": "2026-10-01", "to_date": "2026-10-02",
                                             "template": "## {{me}} {{from}} → {{to}}\n### Started\n### Reviewed"})
    assert status == "WAITING_HUMAN" and run_id.startswith("standup-")
    pc = s.store.run(run_id)
    assert pc["checkpoint"] == "confirm_tickets"
    payload = s._pending(s.graph(run_id).get_state(s.cfg(run_id)))["payload"]
    assert [(t["key"], t["group"]) for t in payload["tickets"]] == [
        ("AQS-1", "started"), ("AQS-3", "started"), ("AQS-4", "started"), ("AQS-13", "reviewed"), ("AQS-10", "reviewed")]
    assert payload["tickets"][0]["url"] == "https://acme.atlassian.net/browse/AQS-1"
    assert payload["range"]["timezone"] == "Asia/Ho_Chi_Minh"
    searches = [a["jql"] for t, a in mcp.calls if t == "jira_search"]
    assert 'status CHANGED FROM "Approved" TO "In Progress" DURING ("2026-09-30", "2026-10-04") AND assignee WAS currentUser()' in searches
    assert 'status CHANGED TO "In Review" DURING ("2026-09-30", "2026-10-04") AND assignee WAS currentUser()' in searches

    assert s.answer(run_id, {"choice": "continue", "keep": ["AQS-1", "AQS-10"]}) == "WAITING_HUMAN"
    digest_prompt = next(p for sch, p, _ in llm.calls if sch is standup.Digest)
    assert "AQS-3" not in digest_prompt and "Checked the CSV filters, LGTM" in digest_prompt and "old question" not in digest_prompt
    report_prompt = next(p for sch, p, _ in llm.calls if sch is standup.Report)
    assert "## Huynh Vong 2026-10-01 → 2026-10-02" in report_prompt
    assert [st for _, _, st in llm.calls] == ["standup.digest", "standup.report"]

    assert s.answer(run_id, {"choice": "regenerate", "note": "shorter"}) == "WAITING_HUMAN"
    assert "shorter" in llm.calls[-1][1]
    assert s.answer(run_id, {"choice": "edit", "report": "my final words"}) == "WAITING_HUMAN"
    assert s.answer(run_id, {"choice": "accept"}) == "COMPLETED"
    saved = Path(ws.state_dir, run_id, "report.md")
    assert saved.read_text() == "my final words\n" and "my final words" in out.getvalue()
    assert {t for t, _ in mcp.calls} <= {"jira_get_user_profile", "jira_search", "jira_get_issue"}  # read-only


def test_template_is_remembered_and_defaults_fill_in(tmp_path):
    s, _, _, _, ws = make_session(tmp_path)
    label, inputs = standup.prepare({"template": "MY TEMPLATE"}, ws)
    assert inputs["template"] == "MY TEMPLATE" and inputs["to_date"] == dt.date.today().isoformat()
    assert inputs["from_date"] == standup.previous_working_day(dt.date.today()).isoformat()
    assert standup.prepare({}, ws)[1]["template"] == "MY TEMPLATE"
    assert standup.prepare({}, None)[1]["template"] == standup.BUILTIN_TEMPLATE.strip()


def test_preflight_needs_search_and_a_sane_range(tmp_path):
    s, out, mcp, _, ws = make_session(tmp_path)
    mcp.tools.discard(ws.jira_tools["search"])
    _, status = s.start_run("standup", {"from_date": "2026-10-03", "to_date": "2026-10-01"})
    assert status == "FAILED"
    assert "after to date" in out.getvalue() and "no JQL search tool" in out.getvalue()


def test_me_from_current_user_and_changelog_fallback(tmp_path):
    ws, _ = make_env(tmp_path, repos=("web",))
    mcp = StandupMcp(list(ws.jira_tools.values()), changelog_in_issue=False)
    jira = JiraGateway(mcp, ws.jira_tools, ws.status_order)
    assert jira.myself("")["account_id"] == "acc-me"  # no jira_user: the assignee of my latest ticket
    act = jira.activity("AQS-10")
    assert [i["field"] for h in act["histories"] for i in h["items"]][-2:] == ["assignee", "status"]
    assert act["histories"][-1]["items"][1]["to_string"] == "In Review"
    assert len(jira.search("anything")) == len(ISSUES)  # paged two at a time until total


def test_abort_at_a_standup_checkpoint(tmp_path):
    s, _, _, _, _ = make_session(tmp_path)
    run_id, _ = s.start_run("standup", {"from_date": "2026-10-01", "to_date": "2026-10-02"})
    assert s.abort(run_id, "not today") == "ABORTED"
