"""Remediation and regression tests for HermesMemoryCompiler."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Script imports
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", [
    "scripts.compile",
    "scripts.lint",
    "scripts.query",
    "scripts.flush",
])
def test_script_imports(module):
    """Each script module must be importable when run from the repo root."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Failed to import {module}: {result.stderr}"


# ---------------------------------------------------------------------------
# 2. KNOWLEDGE_DIR absolute
# ---------------------------------------------------------------------------
def test_knowledge_dir_absolute():
    """KNOWLEDGE_DIR must resolve to an absolute path."""
    import scripts.config as config
    assert config.KNOWLEDGE_DIR.is_absolute()


# ---------------------------------------------------------------------------
# 3. State key format
# ---------------------------------------------------------------------------
def test_state_key_format():
    """compile_daily_log must record ingested logs under a daily/ prefixed key."""
    from scripts.compile import compile_daily_log

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "2026-04-01.md"
        log_path.write_text("# test log\n", encoding="utf-8")
        state = {}

        with (
            patch("scripts.compile.ollama_completion", return_value={
                "choices": [{"message": {"content": "done"}}]
            }),
            patch("scripts.compile.file_hash", return_value="abc123"),
            patch("scripts.compile.save_state"),
        ):
            compile_daily_log(log_path, state)

        assert "daily/2026-04-01.md" in state.get("ingested", {})
        assert state["ingested"]["daily/2026-04-01.md"]["hash"] == "abc123"


# ---------------------------------------------------------------------------
# 4. Marker lifecycle
# ---------------------------------------------------------------------------
def test_marker_lifecycle_preserves_message_count():
    """on_post_llm_call must preserve the existing message_count in the marker."""
    from hermes_memory_compiler.hooks import on_post_llm_call

    with (
        patch("hermes_memory_compiler.hooks.marker.read_marker", return_value={
            "message_count": 5,
            "flush_count": 2,
        }),
        patch("hermes_memory_compiler.hooks.marker.write_marker") as mock_write,
    ):
        on_post_llm_call(
            session_id="test-sess",
            user_message="hi",
            assistant_response="hello",
            conversation_history=[],
            model="test-model",
            platform="test-platform",
        )

        mock_write.assert_called_once()
        args, _ = mock_write.call_args
        data = args[1]
        assert data["message_count"] == 5


# ---------------------------------------------------------------------------
# 5. Finalize deletes marker
# ---------------------------------------------------------------------------
def test_finalize_deletes_marker_on_flush_error():
    """on_session_finalize must delete the marker even if _flush_session raises."""
    from hermes_memory_compiler.hooks import on_session_finalize

    with (
        patch("hermes_memory_compiler.hooks._flush_session", side_effect=RuntimeError("boom")),
        patch("hermes_memory_compiler.hooks.marker.read_marker", return_value={
            "message_count": 3,
        }),
        patch("hermes_memory_compiler.hooks.marker.delete_marker") as mock_delete,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            on_session_finalize(session_id="test-sess", platform="test")

        mock_delete.assert_called_once_with("test-sess", marker_dir=ANY)


# ---------------------------------------------------------------------------
# 6. Flush API guards
# ---------------------------------------------------------------------------
def test_call_ollama_malformed_response():
    """_call_ollama must raise RuntimeError for a response lacking choices."""
    from scripts.flush import _call_ollama

    with pytest.raises(RuntimeError, match="Ollama response missing choices"):
        with patch("scripts.flush.ollama_completion", return_value={}):
            _call_ollama("test prompt")


# ---------------------------------------------------------------------------
# 7. Lock staleness TypeError
# ---------------------------------------------------------------------------
def test_lock_staleness_naive_timestamp():
    """A timezone-naive timestamp in the lock file must be treated as stale."""
    from hermes_memory_compiler.lock import acquire_lock

    with tempfile.TemporaryDirectory() as tmpdir:
        wiki_path = Path(tmpdir)
        lock_path = wiki_path / ".compile.lock"
        stale_data = {
            "agent_name": "other",
            "timestamp": "2026-04-01T12:00:00",
            "pid": 9999,
        }
        lock_path.write_text(json.dumps(stale_data), encoding="utf-8")

        info = acquire_lock(wiki_path, "hermes", timeout_sec=600)
        assert info["agent_name"] == "hermes"
        assert lock_path.exists()
        new_data = json.loads(lock_path.read_text(encoding="utf-8"))
        assert new_data["agent_name"] == "hermes"


# ---------------------------------------------------------------------------
# 8. Lock corrupt JSON (non-dict valid JSON)
# ---------------------------------------------------------------------------
def test_lock_staleness_non_dict_json():
    """A lock file containing valid JSON that is not a dict must be treated as stale."""
    from hermes_memory_compiler.lock import acquire_lock

    with tempfile.TemporaryDirectory() as tmpdir:
        wiki_path = Path(tmpdir)
        lock_path = wiki_path / ".compile.lock"
        lock_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        info = acquire_lock(wiki_path, "hermes", timeout_sec=600)
        assert info["agent_name"] == "hermes"
        assert lock_path.exists()
        new_data = json.loads(lock_path.read_text(encoding="utf-8"))
        assert new_data["agent_name"] == "hermes"


# ---------------------------------------------------------------------------
# 9. _maybe_trigger_compile lock
# ---------------------------------------------------------------------------
def test_maybe_trigger_compile_acquires_lock():
    """_maybe_trigger_compile must call acquire_lock when conditions are met."""
    from scripts.flush import _maybe_trigger_compile

    with tempfile.TemporaryDirectory() as tmpdir:
        daily_path = Path(tmpdir) / "test.md"
        daily_path.write_text("content", encoding="utf-8")

        with (
            patch("scripts.flush.cfg", return_value=0),
            patch("scripts.flush.acquire_lock") as mock_acquire,
            patch("scripts.flush.release_lock"),
            patch("scripts.flush.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            _maybe_trigger_compile(daily_path)

        mock_acquire.assert_called_once()


# ---------------------------------------------------------------------------
# 10. _is_article symlink with outside target
# ---------------------------------------------------------------------------
def test_is_article_symlink_outside_target():
    """_is_article must return True for a symlink inside KNOWLEDGE_DIR whose target is outside."""
    import scripts.lint as lint

    with tempfile.TemporaryDirectory() as tmpdir:
        knowledge_dir = Path(tmpdir) / "knowledge"
        knowledge_dir.mkdir()
        real_file = Path(tmpdir) / "real.md"
        real_file.write_text("# real\n", encoding="utf-8")
        symlink = knowledge_dir / "concepts" / "link.md"
        symlink.parent.mkdir(parents=True)
        symlink.symlink_to(real_file)

        with patch.object(lint, "KNOWLEDGE_DIR", knowledge_dir):
            assert lint._is_article(symlink) is True

        # Also verify it returns True for a regular file inside knowledge_dir
        regular = knowledge_dir / "concepts" / "regular.md"
        regular.write_text("# regular\n", encoding="utf-8")
        with patch.object(lint, "KNOWLEDGE_DIR", knowledge_dir):
            assert lint._is_article(regular) is True


# ---------------------------------------------------------------------------
# 11. on_session_start hook
# ---------------------------------------------------------------------------
def test_on_session_start_hook_exists():
    """on_session_start must be callable and accept the documented signature."""
    from hermes_memory_compiler.hooks import on_session_start
    # Must not raise
    on_session_start("sess-1", "gpt-4", "cli")


# ---------------------------------------------------------------------------
# 12. on_session_end hook
# ---------------------------------------------------------------------------
def test_on_session_end_hook_exists():
    """on_session_end must be callable and accept the documented signature."""
    from hermes_memory_compiler.hooks import on_session_end
    on_session_end("sess-1", completed=True, interrupted=False, model="gpt-4", platform="cli")


# ---------------------------------------------------------------------------
# 13. on_session_reset hook
# ---------------------------------------------------------------------------
def test_on_session_reset_deletes_marker():
    """on_session_reset must delete the session marker."""
    from hermes_memory_compiler.hooks import on_session_reset
    from unittest.mock import patch, ANY

    with patch("hermes_memory_compiler.hooks.marker.delete_marker") as mock_delete:
        on_session_reset("sess-reset-1")
        mock_delete.assert_called_once_with("sess-reset-1", marker_dir=ANY)


# ---------------------------------------------------------------------------
# 14. Flush includes metadata in context
# ---------------------------------------------------------------------------
def test_flush_includes_metadata_in_context():
    """flush_session must include session top-level metadata in the prompt passed to _call_ollama."""
    from scripts.flush import flush_session

    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        sessions_dir = home / ".hermes" / "sessions"
        sessions_dir.mkdir(parents=True)
        marker_dir = home / ".hermes" / "markers"
        marker_dir.mkdir(parents=True)

        session_data = {
            "model": "gpt-4",
            "platform": "openai",
            "session_start": "2026-04-24T01:00:00",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "how are you?"},
            ],
        }
        session_path = sessions_dir / "session_test-sess.json"
        session_path.write_text(json.dumps(session_data), encoding="utf-8")

        def fake_cfg(key, default=None):
            if key == "plugin.marker_dir":
                return str(marker_dir)
            return default

        with (
            patch("scripts.flush.Path.home", return_value=home),
            patch("scripts.flush._should_skip_dedup", return_value=False),
            patch("scripts.flush.cfg", side_effect=fake_cfg),
            patch("scripts.flush._call_ollama", return_value="FLUSH_OK") as mock_call,
        ):
            flush_session("test-sess", dry_run=True)

        mock_call.assert_called_once()
        prompt = mock_call.call_args[0][0]
        assert "**Model:** gpt-4" in prompt
        assert "**Platform:** openai" in prompt
        assert "**Session Start:** 2026-04-24T01:00:00" in prompt


# ---------------------------------------------------------------------------
# 15. pip entry point registered
# ---------------------------------------------------------------------------
def test_pip_entry_point_registered():
    """The hermes-memory-compiler entry point must be registered under hermes_agent.plugins."""
    from importlib.metadata import entry_points

    eps = entry_points(group="hermes_agent.plugins")
    names = {e.name for e in eps}
    assert "hermes-memory-compiler" in names, f"Entry points found: {names}"

