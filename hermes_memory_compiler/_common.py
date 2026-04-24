"""Shared utilities for the Hermes Memory Compiler plugin."""
from __future__ import annotations

from pathlib import Path

DEFAULT_MARKER_DIR = Path("~/.hermes/plugins/hermes-memory-compiler/markers").expanduser()


def resolve_project_root(max_depth: int = 3) -> Path:
    """Locate the project root by searching upward for config.yaml.

    Args:
        max_depth: Maximum number of parent directories to traverse.

    Raises:
        RuntimeError: If config.yaml is not found within *max_depth* levels.
    """
    current = Path(__file__).resolve().parent
    for _ in range(max_depth):
        if (current / "config.yaml").exists():
            return current
        current = current.parent
    raise RuntimeError(
        f"Could not find project root (config.yaml not found within {max_depth} parents)"
    )
