"""Hermes Memory Compiler plugin.

Provides lifecycle hooks for automatic conversation capture and
knowledge-base context injection, plus CLI and slash commands.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import hooks

logger = logging.getLogger(__name__)


def _resolve_project_root() -> Path:
    """Locate project root by finding config.yaml relative to this file."""
    current = Path(__file__).resolve().parent
    for _ in range(3):
        if (current / "config.yaml").exists():
            return current
        current = current.parent
    raise RuntimeError("Could not find project root (config.yaml not found)")


ROOT_DIR = _resolve_project_root()


# ---------------------------------------------------------------------------
# CLI command handler
# ---------------------------------------------------------------------------

def _kb_setup(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes kb <subcommand>`` argparse tree."""
    sub = subparser.add_subparsers(dest="kb_command", help="KB subcommands")

    sub.add_parser("compile", help="Run the knowledge compilation engine")
    sub.add_parser("lint", help="Lint the knowledge base for structural issues")
    query_parser = sub.add_parser("query", help="Query the knowledge base")
    query_parser.add_argument("question", nargs="*", help="Question to ask")
    sub.add_parser("flush", help="Flush completed sessions into the knowledge base")
    sub.add_parser("status", help="Show plugin status and pending markers")


def _kb_handler(args: argparse.Namespace) -> int:
    """Dispatch ``hermes kb`` subcommands."""
    sub = getattr(args, "kb_command", None)

    if sub == "compile":
        return subprocess.run([sys.executable, "scripts/compile.py"], cwd=ROOT_DIR).returncode
    if sub == "lint":
        return subprocess.run([sys.executable, "scripts/lint.py"], cwd=ROOT_DIR).returncode
    if sub == "query":
        question = " ".join(getattr(args, "question", []))
        if not question:
            print("Usage: hermes kb query <question>")
            return 1
        return subprocess.run(
            [sys.executable, "scripts/query.py", question], cwd=ROOT_DIR
        ).returncode
    if sub == "flush":
        return subprocess.run(
            [sys.executable, "scripts/flush.py", "--all"], cwd=ROOT_DIR
        ).returncode
    if sub == "status":
        from . import marker

        # Article count
        knowledge_dir = ROOT_DIR / "knowledge"
        article_count = 0
        if knowledge_dir.exists():
            article_count = sum(
                1 for p in knowledge_dir.rglob("*.md")
                if p.name not in ("index.md", "log.md")
            )

        # State
        state_path = ROOT_DIR / "scripts" / "state.json"
        state: dict = {}
        if state_path.exists():
            with state_path.open("r", encoding="utf-8") as f:
                state = json.load(f)

        ingested = state.get("ingested", {})
        last_compile = "never"
        if ingested:
            timestamps = [
                meta.get("compiled_at") or meta.get("timestamp")
                for meta in ingested.values()
                if (meta.get("compiled_at") or meta.get("timestamp"))
            ]
            if timestamps:
                last_compile = max(timestamps)

        last_lint = state.get("last_lint", "never")
        active_markers = len(marker.list_markers())

        print(f"Articles: {article_count}")
        print(f"Last compile: {last_compile}")
        print(f"Last lint: {last_lint}")
        print(f"Active markers: {active_markers}")
        return 0

    print("Usage: hermes kb {compile,lint,query,flush,status}")
    return 1


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

def _kbq_handler(raw_args: str) -> Optional[str]:
    """Handle ``/kbq <query>`` in-session slash command."""
    query = raw_args.strip()
    if not query:
        return "Usage: /kbq <search query>"
    result = subprocess.run(
        [sys.executable, "scripts/query.py", query],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    if result.returncode != 0 and result.stderr:
        output += f"\n(stderr: {result.stderr.strip()})"
    return output if output else "KB query produced no output."


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the Memory Compiler plugin with Hermes.

    Hooks:
        - pre_llm_call      → inject KB context on first turn
        - post_llm_call     → write session marker after successful turns
        - on_session_finalize → clean up markers at session boundary

    Commands:
        - ``hermes kb`` CLI with subcommands compile, lint, query, flush, status
        - ``/kbq`` in-session slash command
    """
    ctx.register_hook("pre_llm_call", hooks.on_pre_llm_call)
    ctx.register_hook("post_llm_call", hooks.on_post_llm_call)
    ctx.register_hook("on_session_finalize", hooks.on_session_finalize)

    ctx.register_cli_command(
        name="kb",
        help="Knowledge base commands for the Memory Compiler",
        setup_fn=_kb_setup,
        handler_fn=_kb_handler,
        description=(
            "Manage the Hermes Memory Compiler knowledge base.\n\n"
            "Subcommands:\n"
            "  compile  Run the compilation engine\n"
            "  lint     Check KB structure\n"
            "  query    Search the KB\n"
            "  flush    Flush sessions to KB\n"
            "  status   Show plugin status"
        ),
    )

    ctx.register_command(
        name="kbq",
        handler=_kbq_handler,
        description="Query the knowledge base from within a conversation.",
    )
