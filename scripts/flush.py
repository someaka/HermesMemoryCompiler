#!/usr/bin/env python3
"""
Flush Engine for Hermes Memory Compiler.

Reads Hermes session JSON files, compares against marker files, extracts new turns,
calls Ollama to summarize, and appends to the daily log.

Usage:
    python scripts/flush.py --session <ID>
    python scripts/flush.py --all
    python scripts/flush.py --dry-run --all
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from scripts.config import KNOWLEDGE_DIR, ROOT_DIR, cfg, ollama_completion
from hermes_memory_compiler.lock import LockHeldError, acquire_lock, release_lock
from scripts.utils import atomic_json_write, hash_file

SCRIPTS_DIR = ROOT_DIR / "scripts"

# Minimum seconds between flushes of the same session to prevent dupes.
DEDUP_WINDOW_SECONDS = 60

FLUSH_PROMPT = """\
Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.

Format:
**Context:** [One line]
**Key Exchanges:**
- [Important Q&A]
**Decisions Made:**
- [Decisions with rationale]
**Lessons Learned:**
- [Gotchas, patterns, insights]
**Action Items:**
- [Follow-ups or TODOs]

Skip routine tool calls, trivial exchanges.
If nothing worth saving: respond with exactly FLUSH_OK.

## Conversation Context

{context}
"""


def atomic_append(filepath: Path, content: str) -> None:
    """Atomically append content to a file using write-to-temp-then-rename."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if filepath.exists():
                with open(filepath, "r", encoding="utf-8") as src:
                    shutil.copyfileobj(src, f)
            f.write(content)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _call_ollama(prompt: str, dry_run: bool = False) -> Optional[str]:
    """Send a chat-completion request to the Ollama API."""
    if dry_run:
        return "FLUSH_OK"

    resp = ollama_completion(
        messages=[{"role": "user", "content": prompt}],
        temperature=float(cfg("flush.temperature", 0.2)),
        max_tokens=int(cfg("flush.max_tokens", 2048)),
    )
    choices = resp.get("choices")
    if not choices:
        raise RuntimeError("Ollama response missing choices")
    return choices[0].get("message", {}).get("content", "")


def format_messages(messages: list[dict[str, Any]]) -> str:
    """Format user/assistant messages as markdown, stripping reasoning fields."""
    # NOTE: We intentionally read only 'content', not 'reasoning'.
    # Hermes may attach a separate 'reasoning' field to assistant messages;
    # we exclude it from the summarization context per official docs guidance.
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if not content:
            continue
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"**{prefix}:** {content}")
    return "\n\n".join(lines)


def _format_metadata(session_data: dict[str, Any]) -> str:
    """Extract session top-level metadata and format as markdown."""
    lines: list[str] = []
    model = session_data.get("model")
    platform = session_data.get("platform")
    session_start = session_data.get("session_start")
    if model:
        lines.append(f"**Model:** {model}")
    if platform:
        lines.append(f"**Platform:** {platform}")
    if session_start:
        lines.append(f"**Session Start:** {session_start}")
    return "\n".join(lines)


def _should_skip_dedup(session_id: str) -> bool:
    """Read scripts/last-flush.json and skip if same session was flushed within 60 seconds."""
    last_flush_path = SCRIPTS_DIR / "last-flush.json"
    if not last_flush_path.exists():
        return False
    try:
        with open(last_flush_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sessions = data.get("sessions", {})
        last_ts = sessions.get(session_id, "")
        if last_ts:
            last_dt = datetime.fromisoformat(last_ts)
            delta = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if delta < DEDUP_WINDOW_SECONDS:
                return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not read last-flush.json: {e}", file=sys.stderr)
    return False


def _write_last_flush(session_id: str) -> None:
    """Record this flush in scripts/last-flush.json."""
    last_flush_path = SCRIPTS_DIR / "last-flush.json"
    data: dict[str, Any] = {}
    if last_flush_path.exists():
        try:
            with open(last_flush_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read last-flush.json: {e}", file=sys.stderr)
    if "sessions" not in data or not isinstance(data.get("sessions"), dict):
        data["sessions"] = {}
    data["sessions"][session_id] = datetime.now(timezone.utc).isoformat()
    atomic_json_write(last_flush_path, data)


def _maybe_trigger_compile(daily_path: Path) -> None:
    """After a successful flush, auto-trigger compile.py if conditions are met."""
    auto_hour = int(cfg("plugin.auto_compile_hour", 18))
    if datetime.now(timezone.utc).hour < auto_hour:
        return

    try:
        acquire_lock(KNOWLEDGE_DIR, "hermes-flush")
    except LockHeldError:
        print("Compilation lock held; skipping auto-compile.", file=sys.stderr)
        return

    try:
        current_hash = ""
        if daily_path.exists():
            current_hash = hash_file(daily_path)

        state_path = SCRIPTS_DIR / "state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: could not read state.json: {e}", file=sys.stderr)

        last_hash = state.get("last_compile_hash", "")

        if current_hash != last_hash:
            print(f"Auto-compiling (hour >= {auto_hour} and daily log changed)...")
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "compile.py")],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                state["last_compile_hash"] = current_hash
                atomic_json_write(state_path, state)
                print("Auto-compile completed.")
            else:
                print(f"Auto-compile failed: {result.stderr}", file=sys.stderr)
    finally:
        release_lock(KNOWLEDGE_DIR)


def flush_session(session_id: str, dry_run: bool = False) -> Optional[str]:
    """
    Flush a single session.

    Returns the session_id if a summary was generated and appended,
    None if nothing was flushed or FLUSH_OK was returned.
    """
    if _should_skip_dedup(session_id):
        print(f"Skipping {session_id}: flushed within last {DEDUP_WINDOW_SECONDS} seconds.")
        return None

    sessions_dir = Path.home() / ".hermes" / "sessions"
    marker_dir = Path(
        cfg("plugin.marker_dir", str(Path.home() / ".hermes" / "plugins" / "hermes-memory-compiler" / "markers"))
    ).expanduser()
    daily_dir = ROOT_DIR / "daily"

    session_path = sessions_dir / f"session_{session_id}.json"
    marker_path = marker_dir / f"{session_id}.json"

    if not session_path.exists():
        print(f"Session file not found: {session_path}", file=sys.stderr)
        return None

    try:
        with open(session_path, "r", encoding="utf-8") as f:
            session_data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in session file {session_path}: {e}", file=sys.stderr)
        return None

    messages = session_data.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError(f"Invalid messages in session {session_id}: expected list, got {type(messages).__name__}")

    total_count = len(messages)
    marker_count = 0
    flush_count = 0
    if marker_path.exists():
        try:
            with open(marker_path, "r", encoding="utf-8") as f:
                marker_data = json.load(f)
            marker_count = marker_data.get("message_count", 0)
            flush_count = marker_data.get("flush_count", 0)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Invalid marker file for {session_id}: {e}, treating as new", file=sys.stderr)
            marker_count = 0
            flush_count = 0

    if marker_count >= total_count:
        return None

    delta = messages[marker_count:total_count]

    # Filter to user/assistant roles only
    filtered = [m for m in delta if isinstance(m, dict) and m.get("role") in ("user", "assistant")]

    min_turns = int(cfg("flush.min_turns_before_flush", 3))
    if len(filtered) < min_turns:
        return None

    max_msgs = int(cfg("flush.max_messages_per_flush", 50))
    filtered = filtered[:max_msgs]

    metadata_text = _format_metadata(session_data)
    context = format_messages(filtered)
    if metadata_text:
        context = metadata_text + "\n\n" + context
    if not context.strip():
        return None

    prompt = FLUSH_PROMPT.format(context=context)
    response = _call_ollama(prompt, dry_run=dry_run)

    stripped = response.strip()

    now = datetime.now(timezone.utc)

    marker_data = {
        "message_count": total_count,
        "last_flush_timestamp": now.isoformat(),
        "flush_count": flush_count + 1,
    }

    if stripped == "FLUSH_OK":
        if not dry_run:
            marker_dir.mkdir(parents=True, exist_ok=True)
            atomic_json_write(marker_path, marker_data)
            _write_last_flush(session_id)
        else:
            print(f"[DRY-RUN] Would update marker for {session_id} to {total_count}")
        return None

    # Append summary to daily log
    daily_path = daily_dir / f"{now.strftime('%Y-%m-%d')}.md"
    entry = f"""### Session {now.strftime('%H:%M')} - Auto-flushed

{response}

---

"""

    if not dry_run:
        marker_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(marker_path, marker_data)
        _write_last_flush(session_id)
        atomic_append(daily_path, entry)
        _maybe_trigger_compile(daily_path)
    else:
        print(f"[DRY-RUN] Would append to {daily_path}")
        print(entry)

    return session_id


def flush_all(dry_run: bool = False) -> list[str]:
    """
    Scan all session files and flush any with new messages.

    Returns a list of session_ids that were successfully flushed.
    """
    sessions_dir = Path.home() / ".hermes" / "sessions"
    flushed: list[str] = []

    if not sessions_dir.exists():
        print(f"Sessions directory not found: {sessions_dir}", file=sys.stderr)
        return flushed

    for session_file in sorted(sessions_dir.glob("session_*.json")):
        session_id = session_file.stem.replace("session_", "")
        result = flush_session(session_id, dry_run=dry_run)
        if result:
            flushed.append(result)

    return flushed


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Memory Compiler Flush Engine")
    parser.add_argument("--session", metavar="ID", help="Flush a specific session ID")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing files")
    parser.add_argument("--all", action="store_true", help="Flush all sessions with new content")
    args = parser.parse_args()

    if args.session:
        result = flush_session(args.session, dry_run=args.dry_run)
        if result:
            print(f"Flushed session: {result}")
        else:
            print("No flush performed.")
        return 0

    # Default to --all if --dry-run is given without a specific session
    if args.all or args.dry_run:
        flushed = flush_all(dry_run=args.dry_run)
        print(f"Flushed {len(flushed)} session(s): {', '.join(flushed) if flushed else 'none'}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
