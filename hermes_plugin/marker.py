"""Atomic marker file operations for session tracking.

Markers are small JSON files that record per-session metadata
(e.g. message count, last turn timestamp).  They live in the
configured marker_dir and are named ``<session_id>.json``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _marker_path(session_id: str, marker_dir: Path) -> Path:
    """Return the filesystem path for a session marker."""
    # Sanitize session_id to avoid path traversal.
    safe = session_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return marker_dir / f"{safe}.json"


def read_marker(session_id: str, marker_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Read a marker file for *session_id*.

    Args:
        session_id: The Hermes session identifier.
        marker_dir: Directory containing marker files.  If ``None``, the
            default ``~/.hermes/plugins/hermes-memory-compiler/markers``
            is used.

    Returns:
        The parsed JSON dict, or ``None`` if the marker does not exist
        or is unreadable.
    """
    if marker_dir is None:
        marker_dir = Path("~/.hermes/plugins/hermes-memory-compiler/markers").expanduser()

    path = _marker_path(session_id, marker_dir)
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read marker for %s: %s", session_id, exc)
        return None


def write_marker(session_id: str, data: Dict[str, Any], marker_dir: Optional[Path] = None) -> None:
    """Atomically write a marker file for *session_id*.

    Uses write-to-temp-then-rename to ensure readers never see a
    partially-written file.
    """
    if marker_dir is None:
        marker_dir = Path("~/.hermes/plugins/hermes-memory-compiler/markers").expanduser()

    marker_dir.mkdir(parents=True, exist_ok=True)
    path = _marker_path(session_id, marker_dir)

    try:
        fd, tmp = tempfile.mkstemp(dir=str(marker_dir), prefix=".marker_", suffix=".tmp")
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("Failed to write marker for %s: %s", session_id, exc)


def delete_marker(session_id: str, marker_dir: Optional[Path] = None) -> None:
    """Delete the marker file for *session_id* if it exists."""
    if marker_dir is None:
        marker_dir = Path("~/.hermes/plugins/hermes-memory-compiler/markers").expanduser()

    path = _marker_path(session_id, marker_dir)
    try:
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("Failed to delete marker for %s: %s", session_id, exc)


def list_markers(marker_dir: Optional[Path] = None) -> List[str]:
    """Return a list of session_ids that currently have marker files."""
    if marker_dir is None:
        marker_dir = Path("~/.hermes/plugins/hermes-memory-compiler/markers").expanduser()

    if not marker_dir.exists():
        return []

    session_ids: List[str] = []
    for entry in marker_dir.iterdir():
        if entry.is_file() and entry.suffix == ".json" and not entry.name.startswith("."):
            session_ids.append(entry.stem)
    return session_ids
