"""Compilation lock file mechanism for the Hermes Memory Compiler.

Prevents concurrent compilation between Hermes and Claude Code by using
atomic lock files with staleness detection.
"""
from __future__ import annotations

import errno
import json
import os
from datetime import datetime, timezone
from pathlib import Path


class LockHeldError(Exception):
    """Raised when a lock is already held by another agent."""

    def __init__(self, agent_name: str, timestamp: str, pid: int) -> None:
        self.agent_name = agent_name
        self.timestamp = timestamp
        self.pid = pid
        super().__init__(
            f"Lock held by {agent_name} since {timestamp} (pid={pid})"
        )


def acquire_lock(wiki_path: Path, agent_name: str, timeout_sec: int = 600) -> dict:
    """Acquire a compilation lock atomically.

    Args:
        wiki_path: Path to the knowledge base directory.
        agent_name: Name of the agent acquiring the lock (e.g., "hermes", "claude").
        timeout_sec: Maximum age in seconds before a lock is considered stale.

    Returns:
        Lock info dict with keys: agent_name, timestamp, pid.

    Raises:
        LockHeldError: If the lock is held by another agent and is not stale.
    """
    lock_path = Path(wiki_path) / ".compile.lock"

    # Check for existing lock and validate staleness
    if lock_path.exists():
        try:
            with lock_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            existing_agent = data.get("agent_name", "unknown")
            existing_ts = data.get("timestamp", "")
            existing_pid = data.get("pid", 0)

            # Check if lock is stale (>timeout_sec old)
            if existing_ts:
                try:
                    lock_time = datetime.fromisoformat(existing_ts)
                    now = datetime.now(timezone.utc)
                    if (now - lock_time).total_seconds() > timeout_sec:
                        # Stale lock — remove it
                        lock_path.unlink()
                except (ValueError, OSError, TypeError):
                    # Malformed timestamp — treat as stale and remove
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
            else:
                # No timestamp — treat as stale and remove
                lock_path.unlink()

            # If lock still exists after staleness check, it's held
            if lock_path.exists():
                raise LockHeldError(existing_agent, existing_ts, existing_pid)
        except (json.JSONDecodeError, OSError, AttributeError):
            # Corrupt lock file — remove it
            try:
                lock_path.unlink()
            except OSError:
                pass

    # Acquire lock atomically using O_CREAT | O_EXCL
    lock_info = {
        "agent_name": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }

    fd = -1
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(lock_info, f)
    except OSError as exc:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if exc.errno == errno.EEXIST:
            # Race condition: lock was created between our check and open
            # Read the lock and raise
            try:
                with lock_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                raise LockHeldError(
                    data.get("agent_name", "unknown"),
                    data.get("timestamp", ""),
                    data.get("pid", 0),
                )
            except (json.JSONDecodeError, OSError):
                raise LockHeldError("unknown", "", 0)
        raise

    return lock_info


def release_lock(wiki_path: Path) -> None:
    """Release the compilation lock idempotently.

    Args:
        wiki_path: Path to the knowledge base directory.
    """
    lock_path = Path(wiki_path) / ".compile.lock"
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
