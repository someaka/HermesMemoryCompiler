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
from typing import Any

from ._common import DEFAULT_MARKER_DIR

logger = logging.getLogger(__name__)


def _marker_path(session_id: str, marker_dir: Path) -> Path:
    """Return the filesystem path for a session marker."""
    # Sanitize session_id to avoid path traversal.
    safe = session_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return marker_dir / f"{safe}.json"


def read_marker(session_id: str, marker_dir: Path | None = None) -> dict[str, Any] | None:
    """Read a marker file for *session_id*.

    Args:
        session_id: The Hermes session identifier.
        marker_dir: Directory containing marker files.  If ``None``, the
            default ``~/.hermes/plugins/hermes-memory-compiler/markers``
            is used.

    Returns:
        The parsed JSON dict, or ``None`` if the marker does not exist.
        Raises ``json.JSONDecodeError`` if the file is corrupt.
    """
    if marker_dir is None:
        marker_dir = DEFAULT_MARKER_DIR

    path = _marker_path(session_id, marker_dir)
    if not path.exists():
        return None

    return json.loads(path.read_text(encoding="utf-8"))


def write_marker(session_id: str, data: dict[str, Any], marker_dir: Path | None = None) -> None:
    """Atomically write a marker file for *session_id*.

    Uses write-to-temp-then-rename to ensure readers never see a
    partially-written file.
    """
    if marker_dir is None:
        marker_dir = DEFAULT_MARKER_DIR

    marker_dir.mkdir(parents=True, exist_ok=True)
    path = _marker_path(session_id, marker_dir)

    fd, tmp = tempfile.mkstemp(dir=str(marker_dir), prefix=".marker_", suffix=".tmp")
    success = False
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        os.replace(tmp, path)
        success = True
    finally:
        if not success:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def delete_marker(session_id: str, marker_dir: Path | None = None) -> None:
    """Delete the marker file for *session_id* if it exists."""
    if marker_dir is None:
        marker_dir = DEFAULT_MARKER_DIR

    path = _marker_path(session_id, marker_dir)
    if path.exists():
        path.unlink()


def list_markers(marker_dir: Path | None = None) -> list[str]:
    """Return a list of session_ids that currently have marker files."""
    if marker_dir is None:
        marker_dir = DEFAULT_MARKER_DIR

    if not marker_dir.exists():
        return []

    session_ids: list[str] = []
    for entry in marker_dir.iterdir():
        if entry.is_file() and entry.suffix == ".json" and not entry.name.startswith("."):
            session_ids.append(entry.stem)
    return session_ids
