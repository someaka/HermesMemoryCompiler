"""Hermes lifecycle hooks for the Memory Compiler plugin.

- ``pre_llm_call``  → inject KB context into the user message on first turn
- ``post_llm_call`` → write a marker file with conversation metadata
- ``on_session_finalize`` → clean up marker files at session boundary
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import marker
from ._common import DEFAULT_MARKER_DIR, resolve_project_root

logger = logging.getLogger(__name__)

_PROJECT_ROOT = resolve_project_root()
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"

def _load_config() -> dict[str, Any]:
    """Load the Memory Compiler config from config.yaml.

    Raises:
        FileNotFoundError: If config.yaml does not exist.
        yaml.YAMLError: If the file contains invalid YAML.
        OSError: If the file cannot be read.
    """
    import yaml

    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Memory Compiler config not found: {_CONFIG_PATH}")
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get_plugin_config() -> dict[str, Any]:
    """Return the ``plugin`` section of the config with sensible defaults."""
    cfg = _load_config()
    plugin_cfg = cfg.get("plugin", {})
    return {
        "wiki_path": Path(plugin_cfg.get("wiki_path", str(_PROJECT_ROOT / "knowledge"))).expanduser(),
        "marker_dir": Path(plugin_cfg.get("marker_dir", str(DEFAULT_MARKER_DIR))).expanduser(),
        "max_context_chars": int(plugin_cfg.get("max_context_chars", 20000)),
        "max_log_lines": int(plugin_cfg.get("max_log_lines", 30)),
        "auto_flush": bool(plugin_cfg.get("auto_flush", True)),
    }

def _flush_session(session_id: str, cfg: dict[str, Any]) -> None:
    """Run flush.py for a single session via subprocess.

    Uses HERMES_FLUSH_IN_PROGRESS env var as recursion guard.
    Raises on failure so the caller can decide whether to delete the marker.
    """
    if os.environ.get("HERMES_FLUSH_IN_PROGRESS") == "1":
        logger.debug("Flush already in progress, skipping")
        return

    env = os.environ.copy()
    env["HERMES_FLUSH_IN_PROGRESS"] = "1"

    result = subprocess.run(
        [sys.executable, str(_PROJECT_ROOT / "scripts" / "flush.py"), "--session", session_id],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Flush failed for session {session_id}: {result.stderr}")
    logger.info("Flushed session %s", session_id)



def _read_file_lines(path: Path, max_lines: int | None = None) -> str:
    """Read a text file, optionally keeping only the last *max_lines* lines."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")

    if max_lines is None or max_lines <= 0:
        return text

    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def _today_log_path() -> Path:
    """Return the path to today's daily log at the project root."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _PROJECT_ROOT / "daily" / f"{today}.md"


def on_session_start(
    session_id: str,
    model: str,
    platform: str,
    **kwargs: Any,
) -> None:
    """Log session start and initialize tracking.

    Args:
        session_id: The unique session identifier.
        model: The LLM model name for this session.
        platform: The messaging platform (e.g., cli, telegram).
    """
    logger.debug("Session start: %s (model=%s, platform=%s)", session_id, model, platform)


def on_session_end(
    session_id: str,
    completed: bool,
    interrupted: bool,
    model: str,
    platform: str,
    **kwargs: Any,
) -> None:
    """Log session end.

    Note: Final flush and marker cleanup are handled by ``on_session_finalize``
    to avoid double-flushing.
    """
    logger.debug(
        "Session end: %s (completed=%s, interrupted=%s)",
        session_id, completed, interrupted,
    )


def on_session_reset(
    session_id: str,
    **kwargs: Any,
) -> None:
    """Clean up the session marker when the user explicitly resets the session.

    This prevents stale markers from carrying over to the next session
    when the session ID is reused.
    """
    if not session_id:
        raise ValueError("session_id is required for on_session_reset")
    cfg = _get_plugin_config()
    marker_dir = cfg["marker_dir"]
    marker.delete_marker(session_id, marker_dir=marker_dir)
    logger.debug("Session reset: %s", session_id)


def on_pre_llm_call(
    session_id: str,
    user_message: str,
    conversation_history: list[dict[str, Any]],
    is_first_turn: bool,
    model: str,
    platform: str,
    **kwargs: Any,
) -> dict[str, str] | None:
    """Inject KB context into the user message on the first turn only.

    Reads ``wiki_path/index.md`` and today's daily log (last N lines),
    truncates the combined text to ``max_context_chars``, and returns it
    as ephemeral context.  The framework appends this text to the current
    turn's user message — it is never persisted to the session DB.
    """
    if not is_first_turn:
        return None

    cfg = _get_plugin_config()
    wiki_path = cfg["wiki_path"]
    max_context_chars = cfg["max_context_chars"]
    max_log_lines = cfg["max_log_lines"]

    parts: list[str] = []

    # 1. Index / table of contents
    index_path = wiki_path / "index.md"
    index_text = _read_file_lines(index_path)
    if index_text.strip():
        parts.append(index_text)

    # 2. Today's daily log (last N lines)
    log_path = _today_log_path()
    log_text = _read_file_lines(log_path, max_lines=max_log_lines)
    if log_text.strip():
        parts.append(f"---\n## Today's activity log\n{log_text}")

    if not parts:
        return None

    context = "\n\n".join(parts)
    if len(context) > max_context_chars:
        context = context[:max_context_chars]
        # Try to cut at a newline boundary for cleanliness.
        last_nl = context.rfind("\n")
        if last_nl > max_context_chars * 8 // 10:
            context = context[:last_nl]
        context = context + "\n\n[Context truncated]"

    return {"context": context}


def on_post_llm_call(
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list[dict[str, Any]],
    model: str,
    platform: str,
    **kwargs: Any,
) -> None:
    """Write a marker file recording the current conversation state.

    This hook is only reached for successful (non-interrupted) turns —
    the framework guards it with ``if final_response and not interrupted``.
    We add an extra guard here for defensiveness.
    """
    if not assistant_response:
        return

    cfg = _get_plugin_config()
    marker_dir = cfg["marker_dir"]

    existing = marker.read_marker(session_id, marker_dir=marker_dir)
    if existing is None:
        data = {
            "message_count": 0,
            "last_flush_timestamp": datetime.now(timezone.utc).isoformat(),
            "flush_count": 0,
        }
    else:
        data = {
            "message_count": existing.get("message_count", 0),
            "last_flush_timestamp": datetime.now(timezone.utc).isoformat(),
            "flush_count": existing.get("flush_count", 0),
        }
    marker.write_marker(session_id, data, marker_dir=marker_dir)


def on_session_finalize(
    session_id: str | None,
    platform: str,
    **kwargs: Any,
) -> None:
    """Flush and clean up the marker file when the session truly ends.

    ``on_session_finalize`` fires at session boundaries (quit, /new,
    gateway GC). We flush before deleting the marker so the conversation
    is captured in the daily log.
    """
    if not session_id:
        return

    cfg = _get_plugin_config()
    marker_dir = cfg["marker_dir"]

    if not cfg.get("auto_flush", True):
        marker.delete_marker(session_id, marker_dir=marker_dir)
        return

    # Check if there are messages to flush
    marker_data = marker.read_marker(session_id, marker_dir=marker_dir)
    try:
        if marker_data and marker_data.get("message_count", 0) > 0:
            _flush_session(session_id, cfg)
    finally:
        marker.delete_marker(session_id, marker_dir=marker_dir)
