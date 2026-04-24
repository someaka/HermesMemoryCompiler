"""Tests for hermes_memory_compiler hooks."""

import os
from unittest.mock import patch

from hermes_memory_compiler.hooks import _flush_session, _get_plugin_config


def test_flush_session_recursion_guard():
    """_flush_session should skip if HERMES_FLUSH_IN_PROGRESS is set."""
    os.environ["HERMES_FLUSH_IN_PROGRESS"] = "1"
    try:
        with patch("hermes_memory_compiler.hooks.subprocess.run") as mock_run:
            cfg = _get_plugin_config()
            _flush_session("test_session", cfg)
            mock_run.assert_not_called()
    finally:
        del os.environ["HERMES_FLUSH_IN_PROGRESS"]


def test_flush_session_runs_when_not_guarded():
    """_flush_session should call subprocess when guard is not set."""
    # Ensure guard is not set
    os.environ.pop("HERMES_FLUSH_IN_PROGRESS", None)
    with patch("hermes_memory_compiler.hooks.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        cfg = _get_plugin_config()
        _flush_session("test_session", cfg)
        mock_run.assert_called_once()
