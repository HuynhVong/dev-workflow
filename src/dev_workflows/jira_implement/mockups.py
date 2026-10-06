"""Developer-supplied mockups and notes -> one text Design Brief that the planning and coding steps read.

The images are read once (`mockup_brief`, a vision call); every later step gets the brief (a few hundred tokens) instead of
the pictures. Only the coding agent of a UI repo also gets the image paths, so it can open the mockup itself when the words
are not enough. The notes are the developer's own words and win over the ticket when they disagree."""
from pathlib import Path

from ..llm import StructuredLLM, usable_images
from .models import DesignBrief

SYSTEM_BRIEF = (
    "You turn UI mockups, screenshots and a developer's notes into a precise written brief for the engineers who will build "
    "them. Read only what is visible or written: never invent screens, labels or behaviour. Quote visible text verbatim. "
    "Say what is unclear in `ambiguities` instead of guessing."
)


def mockup_images(state: dict) -> list[str]:
    """The developer's images first, then the ticket's own image attachments, existing files only."""
    paths = list(state.get("dev_images") or []) + list((state.get("ticket") or {}).get("images") or [])
    return usable_images(list(dict.fromkeys(paths)))


def make_brief(llm: StructuredLLM, state: dict) -> dict | None:
    """The brief for this run, or None when the developer gave neither images nor notes (no call is made)."""
    images, notes = mockup_images(state), (state.get("dev_notes") or "").strip()
    if not images and not notes:
        return None
    t = state.get("ticket") or {}
    prompt = f"<task>{t.get('title', '')}\n{t.get('description', '')}</task>\n"
    if notes:
        prompt += f"<developer_notes>\n{notes}\n</developer_notes>\n"
    if images:
        prompt += f"{len(images)} image(s) attached.\n"
    prompt += "\nWrite the design brief. The developer notes are the developer's own requirements."
    brief = llm.structured(SYSTEM_BRIEF, prompt, DesignBrief, images=images, step="mockup_brief")
    return {"images": images, **brief.model_dump()}


def _lines(title: str, items: list[str]) -> str:
    return f"{title}:\n" + "\n".join(f"- {i}" for i in items) + "\n" if items else ""


def brief_text(brief: dict | None) -> str:
    """The brief as compact prompt text (no JSON escapes)."""
    if not brief:
        return ""
    return (f"{brief['summary']}\n" + _lines("Screens", brief["screens"]) + _lines("Components", brief["components"])
            + _lines("Visible text", brief["texts"]) + _lines("Styling", brief["styling"])
            + _lines("Interactions", brief["interactions"]))


def context_block(state: dict) -> str:
    """The developer's notes and the brief, for the steps that plan the work (empty when there are none)."""
    parts = []
    if state.get("dev_notes"):
        parts.append(f"<developer_notes priority='highest: these are the developer's own requirements; where they disagree "
                     f"with the ticket text, follow them and say so'>\n{state['dev_notes']}\n</developer_notes>")
    if state.get("design_brief"):
        parts.append(f"<design_brief from='mockups and notes'>\n{brief_text(state['design_brief'])}</design_brief>")
    return "\n".join(parts)


def coding_block(state: dict, has_ui: bool) -> str:
    """What a coding agent gets: the notes for every repo; for a UI repo also the brief and the mockup files to open."""
    if not has_ui:
        return f"<developer_notes>\n{state['dev_notes']}\n</developer_notes>\n" if state.get("dev_notes") else ""
    out = context_block(state) + "\n" if (state.get("dev_notes") or state.get("design_brief")) else ""
    imgs = (state.get("design_brief") or {}).get("images") or []
    if imgs:
        out += ("<mockup_files>\nOpen these with the Read tool when the brief is not enough to match the look; the result must "
                "match them:\n" + "\n".join(f"- {p}" for p in imgs) + "\n</mockup_files>\n")
    return out


def read_dirs(ws, run_id: str) -> list[str]:
    """Folders a coding agent may read besides its repo: this run's mockups and the ticket's attachments."""
    return [str(Path(ws.state_dir, run_id, d)) for d in ("inputs", "attachments")]
