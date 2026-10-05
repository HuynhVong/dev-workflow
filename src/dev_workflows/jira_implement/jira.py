"""Jira through MCP: read the ticket, and the idempotent writes the workflows are allowed (status, one comment per run
and step, and the ticket review's proof screenshots as attachments)."""
import json
import re
from pathlib import Path
from typing import Any

from .mcp_client import McpTools

CONFLUENCE_URL_RE = re.compile(r"https?://[^\s\]\)|\"']*?(?:/wiki/|confluence)[^\s\]\)|\"']*", re.IGNORECASE)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def marker(run_id: str, step: str) -> str:
    return f"[devflow:{run_id}:{step}]"


OPTIONAL_TOOLS = ("attach",)  # never required by `missing_tools`
READ_TOOLS = ("get_issue", "download_attachments")
COMMENT_TOOLS = ("add_comment", "edit_comment")
PERMISSION_RE = re.compile(r"\b(401|403)\b|permission|forbidden|not authori[sz]ed|unauthori[sz]ed|read[- ]only|not allowed", re.I)


class JiraWriteRefused(RuntimeError):
    """Jira (or the MCP server) refused a write: no permission, read-only mode or a missing tool."""


class JiraGateway:
    def __init__(self, mcp: McpTools, tools: dict[str, str], status_order: tuple[str, ...]):
        self.mcp, self.tools, self.status_order = mcp, tools, status_order

    def missing_tools(self) -> list[str]:
        available = set(self.mcp.list_tools())
        return sorted(t for k, t in self.tools.items() if t not in available and k not in OPTIONAL_TOOLS)

    def access_report(self) -> dict:
        """What this server lets devflow do, from its tool list alone (nothing is written to test it):
        {"read": bool, "comment": bool, "attach": bool, "missing_read": [...], "missing_write": [...]}.
        A server in read-only mode hides its write tools, so it shows up here as comment=False."""
        available = set(self.mcp.list_tools())
        miss = lambda keys: [self.tools[k] for k in keys if self.tools.get(k) and self.tools[k] not in available]  # noqa: E731
        missing_read, missing_write = miss(READ_TOOLS), miss(COMMENT_TOOLS[:1])
        return {"read": not missing_read, "comment": not missing_write, "edit": not miss(COMMENT_TOOLS[1:]),
                "attach": bool(self.tools.get("attach")) and not miss(("attach",)),
                "missing_read": missing_read, "missing_write": missing_write}

    def attachment_names(self, key: str) -> list[str]:
        f = self._issue(key, "attachment")
        f = f.get("fields", f)
        return [a.get("filename", "") for a in (f.get("attachment") or f.get("attachments") or []) if isinstance(a, dict)]

    def attach_files(self, key: str, paths: list[str]) -> list[str]:
        """Upload files to the ticket, skipping names it already has (so a retry never duplicates them).
        Returns the file names now on the ticket. Raises JiraWriteRefused when Jira says no."""
        if not paths:
            return []
        if not self.tools.get("attach"):
            raise JiraWriteRefused("no attachment tool configured (jira_tools.attach)")
        have = set(self.attachment_names(key))
        todo = [p for p in paths if Path(p).name not in have]
        if todo:
            self._write(self.tools["attach"], {"issue_key": key, "fields": {}, "attachments": json.dumps(todo)})
        return [Path(p).name for p in paths]

    def _write(self, tool: str, args: dict):
        try:
            return self.mcp.call(tool, args)
        except Exception as e:  # noqa: BLE001 - a refusal is reported, anything else propagates (and is retried)
            if PERMISSION_RE.search(str(e)) or "unknown tool" in str(e).lower():
                raise JiraWriteRefused(str(e)[:500]) from e
            raise

    def _issue(self, key: str, fields: str) -> dict:
        data = self.mcp.call(self.tools["get_issue"], {"issue_key": key, "fields": fields, "comment_limit": 100})
        return data if isinstance(data, dict) else json.loads(data)

    def fetch_ticket(self, key: str, download_dir: str) -> dict:
        """Only what implementation needs: key, title, description, attachments, Confluence links."""
        issue = self._issue(key, "summary,description,attachment,status,assignee")
        f = issue.get("fields", issue)
        description = f.get("description") or ""
        if not isinstance(description, str):
            description = json.dumps(description)
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        if f.get("attachment") or f.get("attachments"):
            self.mcp.call(self.tools["download_attachments"], {"issue_key": key, "target_dir": download_dir})
            files = sorted(str(p) for p in Path(download_dir).iterdir() if p.is_file())
        links = self._remote_links(issue)
        urls = list(dict.fromkeys(links + CONFLUENCE_URL_RE.findall(description)))
        return {
            "key": key,
            "title": f.get("summary", ""),
            "description": description,
            "attachments": files,
            "images": [p for p in files if p.lower().endswith(IMAGE_EXT)],
            "confluence_urls": [u for u in urls if "wiki" in u.lower() or "confluence" in u.lower()],
        }

    @staticmethod
    def _remote_links(issue: dict) -> list[str]:
        out = []
        for link in issue.get("remote_links") or issue.get("remotelinks") or []:
            url = (link.get("object") or {}).get("url") or link.get("url")
            if url:
                out.append(url)
        return out

    def status_and_assignee(self, key: str) -> tuple[str, str]:
        f = self._issue(key, "status,assignee")
        f = f.get("fields", f)
        status = f.get("status") or {}
        assignee = f.get("assignee") or {}
        return (status.get("name", status) if isinstance(status, dict) else str(status),
                (assignee.get("emailAddress") or assignee.get("displayName") or assignee.get("name") or "") if isinstance(assignee, dict) else str(assignee))

    def _rank(self, status: str) -> int:
        lowered = [s.lower() for s in self.status_order]
        return lowered.index(status.lower()) if status.lower() in lowered else -1

    def transition_if_behind(self, key: str, target: str) -> str:
        """Idempotent: only moves forward, and only if the ticket is behind `target`."""
        current, _ = self.status_and_assignee(key)
        if self._rank(current) >= self._rank(target) and self._rank(current) != -1:
            return f"unchanged ({current})"
        transitions = self.mcp.call(self.tools["get_transitions"], {"issue_key": key})
        if isinstance(transitions, dict):
            transitions = transitions.get("transitions", [])
        for t in transitions:
            to = (t.get("to") or {}).get("name") or t.get("to_status") or t.get("name")
            if str(to).lower() == target.lower():
                self.mcp.call(self.tools["transition_issue"], {"issue_key": key, "transition_id": str(t["id"])})
                return f"{current} -> {target}"
        raise RuntimeError(f"No transition from '{current}' to '{target}' on {key}")

    def find_comment(self, key: str, mark: str) -> dict | None:
        issue = self._issue(key, "comment")
        f = issue.get("fields", issue)
        comments = f.get("comment", {})
        comments = comments.get("comments", comments) if isinstance(comments, dict) else comments
        comments = comments or issue.get("comments") or []
        for c in comments:
            if mark in json.dumps(c.get("body", "")):
                return c
        return None

    def upsert_comment(self, key: str, mark: str, body: str) -> str:
        """One comment per (run, step): edit it if the marker is already there, else add it."""
        text = f"{body}\n\n{mark}"
        existing = self.find_comment(key, mark)
        if existing is None:
            self._write(self.tools["add_comment"], {"issue_key": key, "comment": text})
            return "added"
        if self.tools.get("edit_comment"):
            self._write(self.tools["edit_comment"], {"issue_key": key, "comment_id": str(existing.get("id")), "comment": text})
            return "edited"
        return "exists"

    def refresh_delivery_comment(self, key: str, section_mark: str, section: str) -> str:
        """address-review: put `section` (tagged with `section_mark`) into the ticket's single delivery comment,
        replacing an earlier review section, so repeated rounds never stack comments. No status change.
        Falls back to its own marker comment when there is no editable delivery comment."""
        existing = self._find(key, lambda text: DELIVERY_RE.search(text) is not None)
        body = existing.get("body") if existing else None
        if not existing or not isinstance(body, str) or not self.tools.get("edit_comment"):
            return self.upsert_comment(key, section_mark, section)
        delivery = DELIVERY_RE.search(body).group(0)
        head = body.split(REVIEW_SPLIT)[0].replace(delivery, "").rstrip()
        text = f"{head}{REVIEW_SPLIT}{section}\n{section_mark}\n\n{delivery}"
        if text == body:
            return "unchanged"
        self.mcp.call(self.tools["edit_comment"], {"issue_key": key, "comment_id": str(existing.get("id")), "comment": text})
        return "edited"

    def _find(self, key: str, match) -> dict | None:
        issue = self._issue(key, "comment")
        f = issue.get("fields", issue)
        comments = f.get("comment", {})
        comments = comments.get("comments", comments) if isinstance(comments, dict) else comments
        for c in comments or issue.get("comments") or []:
            if match(json.dumps(c.get("body", ""))):
                return c
        return None


DELIVERY_RE = re.compile(r"\[devflow:[^\]\s]+:delivery\]")
REVIEW_SPLIT = "\n\n---\nReview update: "


def attachments_of(ticket: dict[str, Any]) -> list[str]:
    return ticket.get("attachments", [])
