"""Standup / work report: the tickets you started and reviewed in a date range, written into your own report template
(design: docs/standup-design.md).

`devflow standup --from 2026-10-01 --to 2026-10-03 --template weekly.md`

Which tickets count is decided by code from Jira (JQL for candidates, then each ticket's changelog), never by a model:
1. started: the status moved Approved -> In Progress inside the range while the ticket was assigned to you;
2. reviewed: the status moved to In Review inside the range while the ticket was assigned to you, and it is not a
   ticket you started yourself (your own ticket going to review is not a review you did).
"You" is the Jira account behind the Jira MCP. Your comments in the range only add detail to the report.
It is read-only: nothing is written to Jira, git or Confluence. The report is saved locally.
"""
import datetime as dt
import operator
import re
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from .graph import TRANSIENT, Deps, GraphKit, PreflightFailed, _j

WORKFLOW = "standup"
STEPS = ("standup.digest", "standup.report")
MAX_DAYS = 93
DEFAULTS = {"started_from": "Approved", "started_to": "In Progress", "review_status": "In Review"}
TEMPLATE_FILE = "standup-template.md"
BUILTIN_TEMPLATE = """## Work report {{from}} → {{to}}

### Started (Approved → In Progress)
- [KEY] summary: one line on what it is

### Reviewed
- [KEY] summary: what I checked and the outcome

### Notes
"""
SYSTEM = (
    "You write a developer's work report from Jira facts. Use only the tickets you are given; never invent work, "
    "tickets, outcomes or numbers. Keep the user's template exactly: its headings, order, wording and formatting."
)

FORM = [
    {"name": "from_date", "label": "From date", "type": "date", "help": "Inclusive. Empty means the previous working day."},
    {"name": "to_date", "label": "To date", "type": "date", "help": "Inclusive. Empty means today."},
    {"name": "template", "label": "Report template", "type": "textarea", "mono": True,
     "placeholder": BUILTIN_TEMPLATE,
     "help": "Markdown. Empty means the template you used last time (else the built-in one). {{from}}, {{to}} and {{me}} are filled in."},
]
CHECKPOINTS = {"confirm_tickets": "Confirm the tickets", "review_report": "Review the report"}


class TicketLine(BaseModel):
    key: str
    what: str = Field(description="What the ticket is about, in a few plain words.")
    did: str = Field(description="What I did on it: started it, or what I checked in the review and its outcome, from my comments. Empty if unknown.")


class Digest(BaseModel):
    lines: list[TicketLine]


class Report(BaseModel):
    report: str = Field(description="The filled template in Markdown, nothing else.")


class State(TypedDict, total=False):
    run_id: str
    from_date: str
    to_date: str
    template: str
    env_warnings: list[str]
    me: dict
    tz: str
    candidates: dict
    tickets: list[dict]
    keep: list[str]
    digest: list[dict]
    report: str
    report_notes: Annotated[list[str], operator.add]
    report_path: str
    pending_checkpoint: dict | None
    last_answer: dict
    decisions: Annotated[list[dict], operator.add]
    output: str
    aborted_at: str


# --- dates, people and history (pure functions, tested on their own) ---------------------------------------------------
def previous_working_day(today: dt.date) -> dt.date:
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def parse_date(value) -> dt.date | None:
    text = str(value or "").strip()
    return dt.date.fromisoformat(text[:10]) if text else None


def parse_ts(value) -> dt.datetime | None:
    """Jira timestamps: 2026-10-01T09:15:30.000+0700, ...+07:00 or ...Z. A naive one is taken as UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"Z$", "+00:00", text)
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        ts = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def zone(name: str):
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def day_range(start: dt.date, end: dt.date, tz) -> tuple[dt.datetime, dt.datetime]:
    """[start 00:00, end + 1 day 00:00) in `tz` (None = this machine's timezone)."""
    lo = dt.datetime.combine(start, dt.time.min)
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min)
    if tz is None:
        return lo.astimezone(), hi.astimezone()
    return lo.replace(tzinfo=tz), hi.replace(tzinfo=tz)


def identities(me: dict) -> set[str]:
    return {str(v).strip().lower() for k, v in me.items() if k != "time_zone" and v}


def is_me(who, ids: set[str]) -> bool:
    """A user as Jira gives it: a dict (accountId, name, key, emailAddress, displayName) or a bare id or name."""
    if isinstance(who, dict):
        values = [who.get(k) for k in ("accountId", "account_id", "name", "key", "emailAddress", "email", "displayName", "display_name")]
    else:
        values = [who]
    return any(v and str(v).strip().lower() in ids for v in values)


def status_moves(ticket: dict) -> list[dict]:
    """Every status change, oldest first, with the assignee the ticket had right after that history entry (so an
    assignee set in the same move, e.g. on the transition screen, counts): [{at, from, to, assignee: [id, name]}]."""
    histories = ticket["histories"]
    first = next((i for h in histories for i in h["items"] if i["field"] == "assignee"), None)
    if first is not None:
        assignee = [first["from"], first["from_string"]]
    else:
        cur = ticket.get("assignee")
        assignee = [cur] if not isinstance(cur, dict) else [cur.get(k) for k in ("accountId", "name", "emailAddress", "displayName")]
    moves = []
    for h in histories:
        for i in h["items"]:
            if i["field"] == "assignee":
                assignee = [i["to"], i["to_string"]]
        for i in h["items"]:
            if i["field"] == "status":
                moves.append({"at": parse_ts(h["created"]), "from": str(i["from_string"] or ""), "to": str(i["to_string"] or ""),
                              "assignee": list(assignee)})
    return moves


def classify(ticket: dict, ids: set[str], lo: dt.datetime, hi: dt.datetime, statuses: dict) -> dict | None:
    """The ticket's group ("started" or "reviewed") and when it matched, or None. Started wins over reviewed."""
    same = lambda a, b: a.strip().lower() == b.strip().lower()  # noqa: E731
    mine = lambda m: any(is_me(a, ids) for a in m["assignee"])  # noqa: E731
    inside = lambda m: m["at"] is not None and lo <= m["at"] < hi  # noqa: E731
    moves = status_moves(ticket)
    started = [m for m in moves if same(m["from"], statuses["started_from"]) and same(m["to"], statuses["started_to"]) and mine(m)]
    hit = next((m for m in started if inside(m)), None)
    if hit:
        return {"group": "started", "at": hit["at"]}
    if started:  # your own ticket (started by you at any time): moving it to review is not a review you did
        return None
    hit = next((m for m in moves if same(m["to"], statuses["review_status"]) and mine(m) and inside(m)), None)
    return {"group": "reviewed", "at": hit["at"]} if hit else None


def my_comments(ticket: dict, ids: set[str], lo: dt.datetime, hi: dt.datetime, limit: int = 1500) -> list[dict]:
    out = []
    for c in ticket.get("comments") or []:
        at = parse_ts(c.get("created"))
        if at is not None and lo <= at < hi and is_me(c.get("author"), ids):
            body = c.get("body", "")
            body = body if isinstance(body, str) else _j(body)
            out.append({"at": at.isoformat(), "body": body if len(body) <= limit else body[:limit] + "…"})
    return out


def fill_placeholders(template: str, values: dict) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def jira_base_url(ws) -> str:
    for cfg in ws.mcp_servers.values():
        url = str((cfg.get("env") or {}).get("JIRA_URL") or "")
        if url:
            return url.rstrip("/")
    return ""


# --- start inputs ---------------------------------------------------------------------------------------------------
def prepare(values: dict, ws=None) -> tuple[str, dict]:
    """Form values -> (run label, graph inputs). The template you submit is remembered for next time."""
    today = dt.date.today()
    start = parse_date(values.get("from_date") or values.get("from")) or previous_working_day(today)
    end = parse_date(values.get("to_date") or values.get("to")) or today
    template = str(values.get("template") or "").strip()
    remembered = Path(ws.state_dir, TEMPLATE_FILE) if ws else None
    if template and remembered:
        remembered.parent.mkdir(parents=True, exist_ok=True)
        remembered.write_text(template + "\n")
    elif not template:
        template = remembered.read_text().strip() if remembered and remembered.exists() else BUILTIN_TEMPLATE.strip()
    return f"Standup {start} → {end}", {"from_date": start.isoformat(), "to_date": end.isoformat(), "template": template}


DEVFLOW_UI = {
    "title": "Standup",
    "description": "Your work report for a date range: tickets you started and reviewed in Jira, written into your own template.",
    "icon": "calendar", "color": "#4dd0e1", "form": FORM, "checkpoints": CHECKPOINTS, "prepare": prepare,
    "hidden_nodes": ["apply_tickets", "apply_report"],
    "steps": ["preflight", "search_jira", "verify_activity", "confirm_tickets", "digest_tickets", "fill_template",
              "review_report", "save_report"],
    "nodes": {"preflight": "Preflight", "search_jira": "Search Jira", "verify_activity": "Verify activity",
              "confirm_tickets": "Confirm tickets", "digest_tickets": "Digest tickets", "fill_template": "Fill template",
              "review_report": "Review report", "save_report": "Save report", "abort": "Aborted"},
    "node_details": {"preflight": "Jira MCP, your Jira account and the date range",
                     "search_jira": "JQL: Approved → In Progress and → In Review, assigned to you",
                     "verify_activity": "Each ticket's history decides; no AI",
                     "confirm_tickets": "Your decision", "digest_tickets": "One line per ticket",
                     "fill_template": "Your template filled from the facts", "review_report": "Your decision",
                     "save_report": "Saved under .devflow/runs"},
}


# --- the graph ------------------------------------------------------------------------------------------------------
def build_graph(deps: Deps, checkpointer=None):
    ws, store, llm = deps.workspace, deps.store, deps.llm
    statuses = {**DEFAULTS, **{k: str(v) for k, v in ws.standup.items() if k in DEFAULTS and v}}
    g = StateGraph(State)
    kit = GraphKit(g, store)
    node, checkpoint, choice = kit.node, kit.checkpoint, kit.choice

    def bounds(state) -> tuple[dt.datetime, dt.datetime]:
        return day_range(parse_date(state["from_date"]), parse_date(state["to_date"]), zone(state.get("tz", "")))

    # 0 preflight -----------------------------------------------------------------------------------------------
    def preflight(state):
        gaps, warnings = [], []
        start, end = parse_date(state.get("from_date")), parse_date(state.get("to_date"))
        if not start or not end:
            gaps.append("give both dates as YYYY-MM-DD")
        elif start > end:
            gaps.append(f"from date {start} is after to date {end}")
        elif (end - start).days >= MAX_DAYS:
            gaps.append(f"the range is {(end - start).days + 1} days; keep it under {MAX_DAYS}")
        me = {}
        try:
            rep = deps.jira.access_report()
            if not rep["read"]:
                gaps.append(f"Jira MCP cannot read tickets (missing {rep['missing_read']}); run `devflow setup`")
            if not rep["search"]:
                gaps.append(f"Jira MCP has no JQL search tool ('{ws.jira_tools.get('search')}'); map jira_tools.search in workspace.yaml")
            if not gaps:
                me = deps.jira.myself(ws.jira_user)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"Jira MCP: {e}")
        if gaps:
            raise PreflightFailed(gaps)
        tz_name = str(ws.standup.get("timezone") or me.get("time_zone") or "")
        if tz_name and zone(tz_name) is None:
            warnings.append(f"unknown timezone '{tz_name}': using this machine's timezone")
            tz_name = ""
        missing = {s: m for s, m in deps.routing.missing().items() if s in STEPS}
        if missing:
            warnings.append("global Claude Code skills not installed (steps run without them): "
                            + "; ".join(f"{s}: {', '.join(m)}" for s, m in missing.items()))
        store.audit(state["run_id"], "preflight", {"me": me, "timezone": tz_name or "local", "warnings": warnings})
        return {"me": me, "tz": tz_name, "env_warnings": warnings}

    node("preflight", preflight)
    g.add_edge("preflight", "search_jira")

    # 1 candidates by JQL (a day wider each side; the exact range is applied to the history) --------------------------
    def search_jira(state):
        lo = (parse_date(state["from_date"]) - dt.timedelta(days=1)).isoformat()
        hi = (parse_date(state["to_date"]) + dt.timedelta(days=2)).isoformat()
        queries = {
            "started": f'status CHANGED FROM "{statuses["started_from"]}" TO "{statuses["started_to"]}" DURING ("{lo}", "{hi}") '
                       "AND assignee WAS currentUser()",
            "reviewed": f'status CHANGED TO "{statuses["review_status"]}" DURING ("{lo}", "{hi}") AND assignee WAS currentUser()',
        }
        candidates: dict[str, list[str]] = {}
        for group, jql in queries.items():
            for issue in deps.jira.search(jql, fields="summary,status,assignee"):
                key = issue.get("key")
                if key:
                    candidates.setdefault(key, []).append(group)
        store.audit(state["run_id"], "jira_search", {"queries": queries, "candidates": len(candidates)})
        return {"candidates": candidates}

    node("search_jira", search_jira, retry_policy=TRANSIENT)
    g.add_edge("search_jira", "verify_activity")

    # 2 the history decides ---------------------------------------------------------------------------------------
    def verify_activity(state):
        lo, hi = bounds(state)
        ids, base, tickets = identities(state["me"]), jira_base_url(ws), []
        for key in sorted(state.get("candidates") or {}):
            t = deps.jira.activity(key)
            hit = classify(t, ids, lo, hi, statuses)
            if not hit:
                continue
            tickets.append({"key": t["key"], "summary": t["summary"], "status": t["status"], "group": hit["group"],
                            "at": hit["at"].astimezone(lo.tzinfo).isoformat(timespec="minutes"),
                            "url": f"{base}/browse/{t['key']}" if base else "",
                            "description": t["description"][:1500], "my_comments": my_comments(t, ids, lo, hi)})
        tickets.sort(key=lambda t: (t["group"] != "started", t["at"]))
        store.audit(state["run_id"], "activity_verified", {"started": sum(t["group"] == "started" for t in tickets),
                                                          "reviewed": sum(t["group"] == "reviewed" for t in tickets)})
        return {"tickets": tickets}

    node("verify_activity", verify_activity, retry_policy=TRANSIENT)
    g.add_edge("verify_activity", "confirm_tickets")

    def ticket_rows(state) -> list[dict]:
        return [{k: t[k] for k in ("key", "summary", "status", "group", "at", "url")} | {"comments": len(t["my_comments"])}
                for t in state.get("tickets") or []]

    checkpoint("confirm_tickets", lambda s: {
        "range": {"from": s["from_date"], "to": s["to_date"], "timezone": s.get("tz") or "local"},
        "me": s["me"].get("display_name") or s["me"].get("email") or s["me"].get("account_id"),
        "tickets": ticket_rows(s), "warnings": s.get("env_warnings", []), "statuses": statuses,
        "hint": "Untick tickets to leave them out (keep = the keys to use, default all). continue = write the report."},
        ["continue", "abort"])

    def apply_tickets(state):
        keys = [t["key"] for t in state.get("tickets") or []]
        keep = state["last_answer"].get("keep")
        return {"keep": [k for k in keep if k in keys] if isinstance(keep, list) else keys}

    node("apply_tickets", apply_tickets)
    g.add_conditional_edges("confirm_tickets_wait", lambda s: "abort" if choice(s) == "abort" else "apply_tickets",
                            ["abort", "apply_tickets"])
    g.add_edge("apply_tickets", "digest_tickets")

    def kept(state) -> list[dict]:
        keep = set(state.get("keep") or [])
        return [t for t in state.get("tickets") or [] if t["key"] in keep]

    # 3 one line per ticket -----------------------------------------------------------------------------------------
    def digest_tickets(state):
        tickets = kept(state)
        if not tickets:
            return {"digest": []}
        facts = [{k: t[k] for k in ("key", "summary", "group", "description", "my_comments")} for t in tickets]
        d = llm.structured(SYSTEM, f"<tickets>\n{_j(facts)}\n</tickets>\n\nFor each ticket give one line: what it is, and what I did "
                           "on it (started: I started working on it; reviewed: what I checked and the outcome, from my comments).",
                           Digest, step="standup.digest")
        by_key = {line.key: line for line in d.lines}
        return {"digest": [{"key": t["key"], "summary": t["summary"], "group": t["group"], "at": t["at"], "url": t["url"],
                            "what": by_key[t["key"]].what if t["key"] in by_key else t["summary"],
                            "did": by_key[t["key"]].did if t["key"] in by_key else ""} for t in tickets]}

    node("digest_tickets", digest_tickets)
    g.add_edge("digest_tickets", "fill_template")

    # 4 the report --------------------------------------------------------------------------------------------------
    def fill_template(state):
        me = state["me"].get("display_name") or state["me"].get("email") or ""
        template = fill_placeholders(state["template"], {"from": state["from_date"], "to": state["to_date"], "me": me})
        notes = state.get("report_notes") or []
        groups = {g_: [d for d in state.get("digest") or [] if d["group"] == g_] for g_ in ("started", "reviewed")}
        prompt = (f"<template>\n{template}\n</template>\n"
                  f"<started note='moved {statuses['started_from']} → {statuses['started_to']} while assigned to me'>\n{_j(groups['started'])}\n</started>\n"
                  f"<reviewed note='moved to {statuses['review_status']} while assigned to me as the reviewer'>\n{_j(groups['reviewed'])}\n</reviewed>\n"
                  + ("<my_notes>\n" + "\n".join(notes) + "\n</my_notes>\n" if notes else "")
                  + "\nFill the template with these tickets only. Put each ticket in the section that fits its group. Write "
                    "'None' in a section that has no tickets. Keep any example lines' format but replace them with real "
                    "tickets. Return only the filled template.")
        return {"report": llm.structured(SYSTEM, prompt, Report, step="standup.report").report.strip()}

    node("fill_template", fill_template)
    g.add_edge("fill_template", "review_report")

    checkpoint("review_report", lambda s: {
        "report": s["report"], "tickets": len(s.get("digest") or []),
        "hint": "accept = save it. edit = send your edited text in report; regenerate = rewrite it with your note."},
        ["accept", "edit", "regenerate", "abort"])

    def apply_report(state):
        ans = state["last_answer"]
        if ans["choice"] == "edit" and str(ans.get("report") or "").strip():
            return {"report": str(ans["report"]).strip()}
        if ans["choice"] == "regenerate":
            return {"report_notes": [ans.get("note") or "Rewrite the report."]}
        return {}

    node("apply_report", apply_report)
    g.add_conditional_edges("review_report_wait", lambda s: "abort" if choice(s) == "abort" else "apply_report",
                            ["abort", "apply_report"])
    g.add_conditional_edges("apply_report", lambda s: {"accept": "save_report", "edit": "review_report",
                                                       "regenerate": "fill_template"}[choice(s)],
                            ["save_report", "review_report", "fill_template"])

    def save_report(state):
        path = Path(ws.state_dir, state["run_id"], "report.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state["report"] + "\n")
        store.audit(state["run_id"], "report_saved", {"path": str(path)})
        store.set_status(state["run_id"], "COMPLETED", node="save_report")
        return {"report_path": str(path), "output": f"{state['report']}\n\n_Saved to {path}. Nothing was written to Jira._\n"}

    node("save_report", save_report)
    g.add_edge("save_report", END)
    kit.add_abort()
    g.add_edge(START, "preflight")
    return g.compile(checkpointer=checkpointer)
