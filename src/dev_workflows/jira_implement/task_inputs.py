"""What a developer hands a run besides a Jira ticket: mockup/reference images, a note, and (Freely Implement) a branch.

Images arrive in two ways: the UI uploads each one (`save_upload`, base64 over JSON, so no multipart dependency) and the run
then adopts them by id; the CLI passes file paths. Either way a run keeps its own copies under
`<state_dir>/<run_id>/inputs/`, so the mockups stay with the run (and can be shown in the UI) after the upload folder is cleaned.
"""
import base64
import binascii
import re
import shutil
import uuid
from pathlib import Path

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_IMAGES = 6
MAX_BYTES = 5 * 1024 * 1024  # the Anthropic API rejects larger images
PROTECTED = {"develop", "main", "master", "head"}
PROTECTED_PREFIX = ("release/", "hotfix/")


class InputError(ValueError):
    """Bad developer input (a clear message for the form)."""


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name or "image").name).strip("-.") or "image"
    return stem[-80:]


def uploads_dir(ws) -> Path:
    return Path(ws.state_dir, "_uploads")


def inputs_dir(ws, run_id: str) -> Path:
    return Path(ws.state_dir, run_id, "inputs")


def save_upload(ws, name: str, data_b64: str) -> dict:
    """Store one uploaded image; returns {"id", "name", "size"}. The id is what the start form sends back."""
    name = _safe_name(name)
    if not name.lower().endswith(IMAGE_EXT):
        raise InputError(f"{name}: only {', '.join(IMAGE_EXT)} images are accepted")
    try:
        data = base64.b64decode(data_b64.split(",", 1)[-1], validate=False)
    except (binascii.Error, ValueError) as e:
        raise InputError(f"{name}: not valid image data") from e
    if not data:
        raise InputError(f"{name}: the file is empty")
    if len(data) > MAX_BYTES:
        raise InputError(f"{name}: {len(data) // 1024} KB is over the {MAX_BYTES // 1024 // 1024} MB limit")
    uid = uuid.uuid4().hex[:12]
    d = uploads_dir(ws) / uid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return {"id": uid, "name": name, "size": len(data)}


def adopt_images(ws, run_id: str, items) -> list[str]:
    """The run's own copies of the images given as upload ids (UI) or file paths (CLI), in order, absolute paths."""
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if len(items) > MAX_IMAGES:
        raise InputError(f"at most {MAX_IMAGES} images per run (got {len(items)}); each one costs tokens in every step that reads it")
    out_dir = inputs_dir(ws, run_id)
    out: list[str] = []
    for n, item in enumerate(items, start=1):
        up = uploads_dir(ws) / item
        src = next(iter(sorted(up.iterdir())), None) if re.fullmatch(r"[0-9a-f]{12}", item) and up.is_dir() else Path(item).expanduser()
        if src is None or not Path(src).is_file():
            raise InputError(f"image not found: {item}")
        if Path(src).suffix.lower() not in IMAGE_EXT:
            raise InputError(f"{Path(src).name}: only {', '.join(IMAGE_EXT)} images are accepted")
        if Path(src).stat().st_size > MAX_BYTES:
            raise InputError(f"{Path(src).name} is over the {MAX_BYTES // 1024 // 1024} MB limit")
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{n:02d}-{_safe_name(Path(src).name)}"
        shutil.copyfile(src, dst)
        out.append(str(dst))
    for item in items:  # the uploads were only a staging area
        if re.fullmatch(r"[0-9a-f]{12}", item):
            shutil.rmtree(uploads_dir(ws) / item, ignore_errors=True)
    return out


def collect(ws, run_id: str, values: dict) -> dict:
    """Run inputs from the start form: {"dev_images": [paths], "dev_notes": str} (keys only when given)."""
    out: dict = {}
    images = adopt_images(ws, run_id, values.get("images"))
    if images:
        out["dev_images"] = images
    note = str(values.get("note") or "").strip()
    if note:
        out["dev_notes"] = note
    return out


# --- Freely Implement ---------------------------------------------------------------------------------------------------
def validate_branch(name: str, prefix: str = "") -> str:
    """A branch name git accepts that is not a long-lived branch. Returns it trimmed."""
    name = (name or "").strip()
    if not name:
        raise InputError("give a branch name")
    low = name.lower()
    if low in PROTECTED or low.startswith(PROTECTED_PREFIX):
        raise InputError(f"'{name}' is a long-lived branch: pick a new branch name for this work")
    if prefix and not name.startswith(prefix):
        raise InputError(f"branch names here start with '{prefix}' (workspace.yaml: branch_prefix)")
    bad = (name.startswith(("-", "/")) or name.endswith(("/", ".")) or name == "@" or ".." in name or "//" in name or "@{" in name
           or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", name)
           or any(p.startswith(".") or p.endswith(".lock") for p in name.split("/")))
    if bad:  # git check-ref-format's rules (this module never runs git)
        raise InputError(f"'{name}' is not a valid git branch name (no spaces, '..', '~^:?*[\\', leading '-', '/' or '.')")
    return name


def slug(branch: str) -> str:
    """The branch as one folder/key name: `feature/Add-export` -> `feature-add-export`. Worktrees, the one-run-per-key lock and
    the Worktrees page use it, because a name with `/` would nest folders."""
    return re.sub(r"[^a-z0-9._-]+", "-", branch.lower()).strip("-.") or "task"


def title_of(description: str) -> str:
    """A one-line title from the pasted description (its first non-empty line, markdown marks removed)."""
    for line in (description or "").splitlines():
        line = re.sub(r"^[#>*\-\s]+", "", line).strip()
        if line:
            return line[:90]
    return "Task"
