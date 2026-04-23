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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import requests


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
DEFAULT_CONFIG: dict[str, Any] = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "kimi-k2.6:cloud",
    },
    "flush": {
        "temperature": 0.2,
        "max_tokens": 2048,
        "min_turns_before_flush": 3,
        "max_messages_per_flush": 50,
    },
    "plugin": {
        "auto_compile_hour": 18,
    },
}

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


def get_config() -> dict[str, Any]:
    """Load config.yaml from project root, falling back to hard-coded defaults."""
    import yaml
    config_path = ROOT_DIR / "config.yaml"
    config = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                for section in ("ollama", "flush", "compiler", "query", "lint", "plugin"):
                    if section in loaded and isinstance(loaded[section], dict):
                        config.setdefault(section, {})
                        config[section].update(loaded[section])
        except Exception as e:
            print(f"Warning: could not read config.yaml: {e}", file=sys.stderr)
    return config


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
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def atomic_json_write(path: Path, data: object) -> None:
    """Serialize data as JSON and write atomically."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def call_ollama(prompt: str, config: dict[str, Any], dry_run: bool = False) -> Optional[str]:
    """Send a chat-completion request to the Ollama API."""
    if dry_run:
        return "FLUSH_OK"

    base_url = str(config["ollama"].get("base_url", "http://localhost:11434/v1")).rstrip("/")
    model = str(config["ollama"].get("model", "kimi-k2.6:cloud"))
    temperature = float(config["flush"].get("temperature", 0.2))
    max_tokens = int(config["flush"].get("max_tokens", 2048))

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Error calling Ollama: {e}", file=sys.stderr)
        return None


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
            delta = (datetime.now() - last_dt).total_seconds()
            if delta < 60:
                return True
    except (json.JSONDecodeError, OSError, KeyError) as e:
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
    data["sessions"][session_id] = datetime.now().isoformat()
    atomic_json_write(last_flush_path, data)


def _maybe_trigger_compile(daily_path: Path, config: dict[str, Any]) -> None:
    """After a successful flush, auto-trigger compile.py if conditions are met."""
    auto_hour = int(config.get("plugin", {}).get("auto_compile_hour", 18))
    if datetime.now().hour < auto_hour:
        return

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


def flush_session(session_id: str, config: dict[str, Any], dry_run: bool = False) -> Optional[str]:
    """
    Flush a single session.

    Returns the session_id if a summary was generated and appended,
    None if nothing was flushed or FLUSH_OK was returned.
    """
    if _should_skip_dedup(session_id):
        print(f"Skipping {session_id}: flushed within last 60 seconds.")
        return None

    sessions_dir = Path.home() / ".hermes" / "sessions"
    marker_dir = Path.home() / ".hermes" / "plugins" / "hermes-memory-compiler" / "markers"
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
        print(f"Invalid messages in session {session_id}", file=sys.stderr)
        return None

    total_count = session_data.get("message_count", len(messages))
    if total_count != len(messages):
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

    min_turns = int(config["flush"].get("min_turns_before_flush", 3))
    if len(filtered) < min_turns:
        return None

    max_msgs = int(config["flush"].get("max_messages_per_flush", 50))
    filtered = filtered[:max_msgs]

    context = format_messages(filtered)
    if not context.strip():
        return None

    prompt = FLUSH_PROMPT.format(context=context)
    response = call_ollama(prompt, config, dry_run=dry_run)

    if response is None:
        print(f"Ollama call failed for session {session_id}; marker not updated.", file=sys.stderr)
        return None

    stripped = response.strip()

    now = datetime.now()

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
        atomic_append(daily_path, entry)
        marker_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(marker_path, marker_data)
        _write_last_flush(session_id)
        _maybe_trigger_compile(daily_path, config)
    else:
        print(f"[DRY-RUN] Would append to {daily_path}")
        print(entry)

    return session_id


def flush_all(config: dict[str, Any], dry_run: bool = False) -> list[str]:
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
        result = flush_session(session_id, config, dry_run=dry_run)
        if result:
            flushed.append(result)

    return flushed


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Memory Compiler Flush Engine")
    parser.add_argument("--session", metavar="ID", help="Flush a specific session ID")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing files")
    parser.add_argument("--all", action="store_true", help="Flush all sessions with new content")
    args = parser.parse_args()

    config = get_config()

    if args.session:
        result = flush_session(args.session, config, dry_run=args.dry_run)
        if result:
            print(f"Flushed session: {result}")
        else:
            print("No flush performed.")
        return 0

    # Default to --all if --dry-run is given without a specific session
    if args.all or args.dry_run:
        flushed = flush_all(config, dry_run=args.dry_run)
        print(f"Flushed {len(flushed)} session(s): {', '.join(flushed) if flushed else 'none'}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
