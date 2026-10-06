"""Strictly read-only Confluence access. Write tools are unreachable by construction."""
import html
import re
from typing import Any

from .mcp_client import McpTools

# Any tool whose name contains one of these verbs is a write tool, whatever the server calls it.
WRITE_VERBS = re.compile(
    r"(create|update|edit|delete|remove|add|move|copy|publish|upload|write|put|post|comment|label|archive|restore|set|patch|attach|rename)",
    re.IGNORECASE,
)


class ConfluenceWriteBlocked(PermissionError):
    pass


def is_write_tool(name: str) -> bool:
    bare = name.split("__")[-1]
    return bool(WRITE_VERBS.search(bare.replace("get_attachments", "get_files")))


class ConfluenceReader:
    """Exposes ONLY the configured read tools. A configured name that looks like a write tool is rejected."""

    def __init__(self, mcp: McpTools, read_tools: dict[str, str]):
        bad = [t for t in read_tools.values() if is_write_tool(t)]
        if bad:
            raise ConfluenceWriteBlocked(f"refusing write-like tools in the Confluence read allow-list: {bad}")
        self._mcp = mcp
        self._tools = dict(read_tools)

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(self._tools.values())

    def _call(self, op: str, args: dict[str, Any]) -> Any:
        tool = self._tools[op]
        if is_write_tool(tool) or tool not in self.allowed_tools:
            raise ConfluenceWriteBlocked(tool)
        return self._mcp.call(tool, args)

    def get_page(self, page_id: str) -> Any:
        return self._call("get_page", {"page_id": page_id})

    def get_children(self, page_id: str) -> Any:
        return self._call("get_page_children", {"parent_id": page_id})

    def search(self, query: str, limit: int = 5) -> Any:
        return self._call("search", {"query": query, "limit": limit})

    def missing_tools(self) -> list[str]:
        return self.tool_report()[0]

    def tool_report(self) -> tuple[list[str], list[str]]:
        """(read tools this server lacks, Confluence write tools it offers that devflow blocks). Never calls a tool."""
        available = set(self._mcp.list_tools())
        missing = sorted(t for t in self._tools.values() if t not in available)
        blocked = sorted(t for t in available if "confluence" in t.lower() and is_write_tool(t))
        return missing, blocked


PAGE_ID_RE = re.compile(r"/pages/(?:viewpage\.action\?pageId=)?(\d+)|pageId=(\d+)")


def page_ids_from_urls(urls: list[str]) -> list[str]:
    ids = []
    for u in urls:
        m = PAGE_ID_RE.search(u)
        if m:
            pid = m.group(1) or m.group(2)
            if pid not in ids:
                ids.append(pid)
    return ids


# Page fields that only cost tokens in a prompt (links, versions, permissions, expansion hints).
NOISE_KEYS = {"_links", "_expandable", "links", "version", "history", "extensions", "metadata", "operations",
              "restrictions", "container", "ancestors", "space", "body_format", "webui", "tinyui", "self", "icon"}
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"<\s*(?:/p|br\s*/?|/h\d|/li|/tr|/div|/table|/ul|/ol)\s*>", re.I)


def html_to_text(text: str) -> str:
    """Confluence storage-format HTML -> plain text with line breaks kept (tables become one row per line)."""
    text = re.sub(r"<\s*/t[dh]\s*>", " | ", text, flags=re.I)
    text = _BLOCK.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def compact_pages(obj: Any) -> Any:
    """Confluence results trimmed for a prompt: HTML bodies as text, link/version/permission fields dropped."""
    if isinstance(obj, dict):
        return {k: compact_pages(v) for k, v in obj.items() if k not in NOISE_KEYS and v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [compact_pages(v) for v in obj]
    if isinstance(obj, str) and _TAG.search(obj):
        return html_to_text(obj)
    return obj
