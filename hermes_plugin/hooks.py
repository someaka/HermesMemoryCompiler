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
from typing import Any, Dict, List, Optional

from . import marker

logger = logging.getLogger(__name__)

def _resolve_project_root() -> Path:
    """Locate project root by finding config.yaml relative to this file."""
    current = Path(__file__).resolve().parent
    for _ in range(3):
        if (current / "config.yaml").exists():
            return current
        current = current.parent
    raise RuntimeError("Could not find project root (config.yaml not found)")

_PROJECT_ROOT = _resolve_project_root()
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_DEFAULT_MARKER_DIR = Path("~/.hermes/plugins/hermes-memory-compiler/markers").expanduser()


def _load_config() -> Dict[str, Any]:
    """Load the Memory Compiler config, returning an empty dict on failure."""
    try:
        import yaml
        if _CONFIG_PATH.exists():
            return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("Failed to load config from %s: %s", _CONFIG_PATH, exc)
    return {}


def _get_plugin_config() -> Dict[str, Any]:
    """Return the ``plugin`` section of the config with sensible defaults."""
    cfg = _load_config()
    plugin_cfg = cfg.get("plugin", {})
    return {
        "wiki_path": Path(plugin_cfg.get("wiki_path", str(_PROJECT_ROOT / "knowledge"))).expanduser(),
        "marker_dir": Path(plugin_cfg.get("marker_dir", str(_DEFAULT_MARKER_DIR))).expanduser(),
        "max_context_chars": int(plugin_cfg.get("max_context_chars", 20000)),
        "max_log_lines": int(plugin_cfg.get("max_log_lines", 30)),
        "auto_flush": bool(plugin_cfg.get("auto_flush", True)),
    }

def _flush_session(session_id: str, cfg: Dict[str, Any]) -> None:
    """Run flush.py for a single session via subprocess.

    Uses HERMES_FLUSH_IN_PROGRESS env var as recursion guard.
    """
    if os.environ.get("HERMES_FLUSH_IN_PROGRESS"):
        logger.debug("Flush already in progress, skipping")
        return

    env = os.environ.copy()
    env["HERMES_FLUSH_IN_PROGRESS"] = "1"

    try:
        result = subprocess.run(
            [sys.executable, str(_PROJECT_ROOT / "scripts" / "flush.py"), "--session", session_id],
            cwd=_PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.error("Flush failed for session %s: %s", session_id, result.stderr)
        else:
            logger.info("Flushed session %s", session_id)
    except subprocess.TimeoutExpired:
        logger.error("Flush timed out for session %s", session_id)
    except Exception as exc:
        logger.error("Flush exception for session %s: %s", session_id, exc)



def _read_file_lines(path: Path, max_lines: Optional[int] = None) -> str:
    """Read a text file, optionally keeping only the last *max_lines* lines."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.debug("Failed to read %s: %s", path, exc)
        return ""

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


def on_pre_llm_call(
    session_id: str,
    user_message: str,
    conversation_history: List[Dict[str, Any]],
    is_first_turn: bool,
    model: str,
    platform: str,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
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

    parts: List[str] = []

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
        if last_nl > max_context_chars * 0.8:
            context = context[:last_nl]
        context = context + "\n\n[Context truncated]"

    return {"context": context}


def on_post_llm_call(
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: List[Dict[str, Any]],
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

    data = {
        "message_count": len(conversation_history),
        "last_flush_timestamp": datetime.now(timezone.utc).isoformat(),
        "flush_count": 0,
    }
    marker.write_marker(session_id, data, marker_dir=marker_dir)


def on_session_finalize(
    session_id: Optional[str],
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
    if marker_data and marker_data.get("message_count", 0) > 0:
        _flush_session(session_id, cfg)

    # Clean up marker regardless of flush success/failure
    marker.delete_marker(session_id, marker_dir=marker_dir)
