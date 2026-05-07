"""Shared utilities for the Hermes Memory Compiler plugin."""
from __future__ import annotations

import os
from pathlib import Path


def get_hermes_home() -> Path:
    """Return the Hermes home directory, honoring HERMES_HOME env var.

    Falls back to ``Path.home() / '.hermes'`` when HERMES_HOME is unset.
    This mirrors ``hermes_constants.get_hermes_home()`` so the plugin
    stays consistent with the host Hermes installation — especially
    important in profile mode where ``HOME`` may be redirected.
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    return Path.home() / ".hermes"


DEFAULT_MARKER_DIR = get_hermes_home() / "plugins" / "hermes-memory-compiler" / "markers"


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
