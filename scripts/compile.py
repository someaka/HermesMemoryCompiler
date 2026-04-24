"""
Compile daily conversation logs into structured knowledge articles.

Usage:
    python scripts/compile.py              # compile new/changed logs only
    python scripts/compile.py --all        # force recompile everything
    python scripts/compile.py --file daily/2026-04-01.md
    python scripts/compile.py --dry-run    # show what would be compiled
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from scripts.config import DAILY_DIR, KNOWLEDGE_DIR, ROOT_DIR, cfg, ollama_completion
from hermes_memory_compiler.lock import LockHeldError, acquire_lock, release_lock
from scripts.utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    now_iso,
    read_wiki_index,
    save_state,
    today_iso,
)

AGENTS_FILE = ROOT_DIR / "AGENTS.md"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"

# OpenAI-compatible tool definitions for the compiler
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file (creates parent dirs)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace old_string with new_string in a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a regex pattern in files under a path",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern", "path"],
            },
        },
    },
]


def _tool_read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, PermissionError, UnicodeDecodeError) as e:
        return f"Error reading {path}: {e}"


def _tool_write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"
    except (OSError, PermissionError) as e:
        return f"Error writing {path}: {e}"


def _tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: file not found: {path}"
        text = p.read_text(encoding="utf-8")
        if old_string not in text:
            return f"Error: old_string not found in {path}"
        text = text.replace(old_string, new_string, 1)
        p.write_text(text, encoding="utf-8")
        return f"Edited {path}"
    except (OSError, PermissionError, UnicodeDecodeError) as e:
        return f"Error editing {path}: {e}"


def _tool_glob(pattern: str) -> str:
    import glob as _glob
    matches = _glob.glob(pattern, recursive=True)
    return "\n".join(matches) if matches else "(no matches)"


def _tool_grep(pattern: str, path: str) -> str:
    import re as _re
    root = Path(path)
    if not root.exists():
        return f"Error: path not found: {path}"
    results = []
    try:
        files = list(root.rglob("*.md")) if root.is_dir() else [root]
        for f in files:
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if _re.search(pattern, line):
                        results.append(f"{f}:{i}:{line}")
            except (OSError, PermissionError, UnicodeDecodeError) as e:
                results.append(f"{f}:Error reading file: {e}")
    except (OSError, PermissionError) as e:
        return f"Error during grep: {e}"
    return "\n".join(results[:100]) if results else "(no matches)"


def execute_tool(call: dict) -> str:
    try:
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"])
    except (KeyError, json.JSONDecodeError) as e:
        return f"Error: malformed tool call: {e}"
    if name == "read_file":
        path = args.get("path")
        if path is None:
            return "Error: read_file requires 'path'"
        return _tool_read_file(path)
    if name == "write_file":
        path = args.get("path")
        content = args.get("content")
        if path is None or content is None:
            return "Error: write_file requires 'path' and 'content'"
        return _tool_write_file(path, content)
    if name == "edit_file":
        path = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if path is None or old_string is None or new_string is None:
            return "Error: edit_file requires 'path', 'old_string', and 'new_string'"
        return _tool_edit_file(path, old_string, new_string)
    if name == "glob":
        pattern = args.get("pattern")
        if pattern is None:
            return "Error: glob requires 'pattern'"
        return _tool_glob(pattern)
    if name == "grep":
        pattern = args.get("pattern")
        path = args.get("path")
        if pattern is None or path is None:
            return "Error: grep requires 'pattern' and 'path'"
        return _tool_grep(pattern, path)
    return f"Unknown tool: {name}"


def compile_daily_log(log_path: Path, state: dict) -> None:
    """Compile a single daily log into knowledge articles."""
    log_content = log_path.read_text(encoding="utf-8")
    if not AGENTS_FILE.exists():
        print(f"Error: AGENTS.md not found at {AGENTS_FILE}")
        return
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()
    timestamp = now_iso()

    system_prompt = (
        "You are a knowledge compiler. Your job is to read a daily conversation log "
        "and extract knowledge into structured wiki articles. "
        "You have access to file tools. Use them precisely. "
        "Write complete YAML frontmatter for every article. "
        "Use Obsidian-style [[path/to/article]] wikilinks. "
        "When done, do not make further tool calls. "
        "Every article you create or update must include tags: [Hermes] in its YAML frontmatter, preserving any existing tags."
    )

    prompt = f"""## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{wiki_index if wiki_index else "(No existing articles yet)"}

## Daily Log to Compile

**File:** {log_path.name}

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article
2. **Create concept articles** in `knowledge/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Use read_file to read the existing article, then edit_file or write_file to update it
   - Add the daily log source to the article's frontmatter
5. **Update knowledge/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {today_iso()} |`
6. **Append to knowledge/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```
7. **Tag all articles** - Every concept, connection, and Q&A article must have `tags: [Hermes]` in its YAML frontmatter. When updating existing articles, append `Hermes` to the existing tags list without removing other tags.

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    max_turns = int(cfg("compiler.max_turns", 30))
    for turn in range(1, max_turns + 1):
        try:
            resp = ollama_completion(
                messages=messages,
                tools=TOOLS,
                temperature=float(cfg("compiler.temperature", 0.3)),
                max_tokens=int(cfg("compiler.max_tokens", 4096)),
            )
        except RuntimeError as e:
            print(f"  API error: {e}")
            return

        choices = resp.get("choices")
        if not choices:
            print("  API error: Ollama returned empty choices")
            return
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            # Done
            break

        # Execute tool calls and append results
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })

        for call in tool_calls:
            tool_call_id = call.get("id", "unknown")
            result = execute_tool(call)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })
    else:
        print(f"  Warning: reached max_turns ({max_turns})")
        return

    # Update state
    rel_path = f"daily/{log_path.name}"
    state.setdefault("ingested", {})[rel_path] = {
        "hash": file_hash(log_path),
        "compiled_at": now_iso(),
    }
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    try:
        acquire_lock(KNOWLEDGE_DIR, "hermes")
    except LockHeldError as exc:
        print(f"Error: Compilation lock held by {exc.agent_name} since {exc.timestamp} (pid {exc.pid})")
        sys.exit(2)

    try:
        state = load_state()

        if args.file:
            target = Path(args.file)
            if not target.is_absolute():
                target = DAILY_DIR / target.name
            if not target.exists():
                target = ROOT_DIR / args.file
            if not target.exists():
                print(f"Error: {args.file} not found")
                sys.exit(1)
            to_compile = [target]
        else:
            all_logs = list_raw_files()
            if args.all:
                to_compile = all_logs
            else:
                to_compile = []
                for log_path in all_logs:
                    rel = f"daily/{log_path.name}"
                    prev = state.get("ingested", {}).get(rel, {})
                    if not prev or prev.get("hash") != file_hash(log_path):
                        to_compile.append(log_path)

        if not to_compile:
            print("Nothing to compile - all daily logs are up to date.")
            return

        print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
        for f in to_compile:
            print(f"  - {f.name}")

        if args.dry_run:
            return

        for i, log_path in enumerate(to_compile, 1):
            print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
            compile_daily_log(log_path, state)

            if f"daily/{log_path.name}" not in state.get("ingested", {}):
                print(f"  Warning: compilation did not complete for {log_path.name}; skipping archive.")
                continue

            # Archive the daily log after successful compilation
            archive_dir = DAILY_DIR / "archive"
            archive_path = archive_dir / log_path.name
            archive_dir.mkdir(parents=True, exist_ok=True)
            if archive_path.exists():
                print(f"  Warning: {archive_path.name} already exists in archive, overwriting.")
            shutil.move(str(log_path), str(archive_path))
            print(f"  Archived {log_path.name} -> daily/archive/{log_path.name}")
            print("  Done.")

        articles = list(list_wiki_articles())
        print(f"\nCompilation complete.")
        print(f"Knowledge base: {len(articles)} articles")
    finally:
        release_lock(KNOWLEDGE_DIR)


if __name__ == "__main__":
    main()
