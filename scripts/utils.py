"""Shared helpers for Hermes Memory Compiler."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import string
import tempfile
from datetime import datetime, timezone
from typing import Iterator

from config import KNOWLEDGE_DIR

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def list_wiki_articles() -> Iterator[pathlib.Path]:
    """Yield all .md files under knowledge/."""
    if not KNOWLEDGE_DIR.exists():
        return
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        yield path


def extract_wikilinks(text: str) -> list[str]:
    """Find all [[...]] patterns and return the inner paths."""
    return _WIKILINK_RE.findall(text)


def atomic_write(path: str | os.PathLike, content: str) -> None:
    """Write to a temp file in the same directory, then rename atomically."""
    dest = pathlib.Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".tmp-{dest.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_json_write(path: str | os.PathLike, data: object) -> None:
    """Serialize data as JSON and write atomically."""
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def hash_file(path: str | os.PathLike) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def read_wiki_index() -> str:
    """Read knowledge/index.md, returning empty string if missing."""
    index_path = KNOWLEDGE_DIR / "index.md"
    if not index_path.exists():
        return ""
    return index_path.read_text(encoding="utf-8")


def read_all_wiki_content() -> str:
    """Read all wiki articles and return them as a formatted string."""
    parts = []
    for path in list_wiki_articles():
        rel = path.relative_to(KNOWLEDGE_DIR)
        content = path.read_text(encoding="utf-8")
        parts.append(f"### {rel}\n```markdown\n{content}\n```")
    if not parts:
        return ""
    return "\n\n".join(parts)


def slugify(text: str) -> str:
    """Lowercase, strip special chars, replace spaces with hyphens."""
    text = text.lower()
    allowed = set(string.ascii_lowercase + string.digits + " -")
    cleaned = "".join(ch for ch in text if ch in allowed)
    cleaned = cleaned.strip().replace(" ", "-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned


def now_iso() -> str:
    """Current ISO 8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def today_iso() -> str:
    """Current date YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Aliases and state helpers used by compile.py ────────────────────────

file_hash = hash_file


def list_raw_files() -> list[pathlib.Path]:
    """Return all .md files in the daily/ directory, sorted by name."""
    from config import DAILY_DIR

    if not DAILY_DIR.exists():
        return []
    return sorted(DAILY_DIR.glob("*.md"))


def load_state() -> dict:
    """Load compilation state from scripts/state.json."""
    from config import STATE_PATH

    if not STATE_PATH.exists():
        return {"ingested": {}, "total_cost": 0.0}
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(data: dict) -> None:
    """Save compilation state to scripts/state.json."""
    from config import STATE_PATH

    atomic_json_write(STATE_PATH, data)
