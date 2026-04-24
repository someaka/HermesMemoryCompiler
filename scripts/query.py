#!/usr/bin/env python3
"""Index-guided query against the Hermes Memory Compiler knowledge base.

Reads the master index, asks the LLM to select relevant articles, then
synthesizes an answer. With --file-back, persists the result to knowledge/qa/.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys
from datetime import timezone
from typing import Any

from pathlib import Path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from scripts.config import KNOWLEDGE_DIR, ROOT_DIR, cfg, ollama_completion
from hermes_memory_compiler.lock import LockHeldError, acquire_lock, release_lock
from scripts.utils import atomic_write, extract_wikilinks, list_wiki_articles, read_wiki_index


def _sanitize_filename(text: str) -> str:
    """Create a safe filename from arbitrary text."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text[:80].strip("-")


def _build_prompt(question: str, index_text: str, articles_text: str) -> str:
    return (
        "You are a knowledge base query engine. Your job is to answer questions "
        "using the provided knowledge base articles.\n\n"
        "## Knowledge Base Index\n\n"
        f"{index_text}\n\n"
        "## Selected Articles\n\n"
        f"{articles_text}\n\n"
        "## Instructions\n"
        "1. Synthesize a concise, accurate answer to the user's question.\n"
        "2. Cite sources using Obsidian-style [[path/to/article]] wikilinks.\n"
        "3. If the knowledge base does not contain enough information, say so clearly.\n\n"
        f"## Question\n\n{question}\n"
    )


def _select_articles_via_llm(question: str, index_text: str) -> list[str]:
    """Ask the LLM which articles from the index are relevant."""
    prompt = (
        "You are a retrieval engine. Given the knowledge base index below, "
        "select the 3-10 most relevant article paths to answer the question.\n\n"
        "## Index\n\n"
        f"{index_text}\n\n"
        "## Question\n\n"
        f"{question}\n\n"
        "Return ONLY a JSON array of article paths (e.g. [\"concepts/example\"]). "
        "Do not include .md extensions. If no articles are relevant, return []."
    )
    messages = [
        {"role": "system", "content": "You output only valid JSON arrays of strings."},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = ollama_completion(
            messages=messages,
            temperature=cfg("query.temperature", 0.2),
            max_tokens=cfg("query.max_tokens", 2048),
        )
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # Try to parse the entire response first; fall back to regex extraction
        # because some models wrap the JSON in markdown fences or prose.
        stripped = content.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            paths = json.loads(stripped)
            if isinstance(paths, list):
                return [str(p) for p in paths]
        match = re.search(r"\[.*?\]", content, re.DOTALL)
        if match:
            paths = json.loads(match.group(0))
            if isinstance(paths, list):
                return [str(p) for p in paths]
    except (RuntimeError, json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"Article selection failed: {exc}") from exc
    return []


def _read_article(path_str: str) -> str:
    """Read an article by path (with or without .md)."""
    for suffix in ("", ".md"):
        p = KNOWLEDGE_DIR / (path_str + suffix)
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def run_query(question: str, file_back: bool = False) -> str:
    index_text = read_wiki_index()
    if not index_text:
        return "Error: knowledge/index.md is empty or missing."

    selected = _select_articles_via_llm(question, index_text)
    articles_text = ""
    consulted: list[str] = []
    for path_str in selected:
        text = _read_article(path_str)
        if text:
            articles_text += f"\n--- {path_str} ---\n{text}\n"
            consulted.append(path_str)

    # If no articles selected or all missing, include a note
    if not consulted:
        articles_text = "(No specific articles selected or found.)"

    prompt = _build_prompt(question, index_text, articles_text)
    messages = [
        {"role": "system", "content": "You are a precise knowledge base assistant."},
        {"role": "user", "content": prompt},
    ]

    resp = ollama_completion(
        messages=messages,
        temperature=cfg("query.temperature", 0.2),
        max_tokens=cfg("query.max_tokens", 2048),
    )
    choices = resp.get("choices")
    if not choices:
        return "Error: Ollama returned empty choices."
    answer = choices[0].get("message", {}).get("content", "")

    if file_back:
        try:
            acquire_lock(KNOWLEDGE_DIR, "hermes")
        except LockHeldError as exc:
            print(
                f"Warning: could not file back answer — lock held by {exc.agent_name} "
                f"since {exc.timestamp} (pid {exc.pid})",
                file=sys.stderr,
            )
            return answer

        try:
            today = datetime.date.today().isoformat()
            slug = _sanitize_filename(question)
            filename = f"{today}-{slug}.md"
            qa_path = KNOWLEDGE_DIR / "qa" / filename
            frontmatter = (
                f"---\n"
                f"title: \"Q: {question}\"\n"
                f"question: \"{question}\"\n"
                f"consulted:\n"
                + "".join(f"  - \"{c}\"\n" for c in consulted)
                + f"filed: {today}\n"
                "---\n\n"
            )
            content = frontmatter + f"# Q: {question}\n\n## Answer\n\n{answer}\n\n## Sources Consulted\n\n"
            for c in consulted:
                content += f"- [[{c}]]\n"
            atomic_write(qa_path, content)
            print(f"Filed answer to: {qa_path.relative_to(ROOT_DIR)}")

            # Update knowledge/index.md
            index_path = KNOWLEDGE_DIR / "index.md"
            index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
            # Escape pipe characters in the question to avoid breaking the markdown table.
            safe_question = question.replace("|", "\\|")
            row = f"| [[qa/{today}-{slug}]] | Answer to: {safe_question} | query | {today} |"
            if not index_text.endswith("\n"):
                index_text += "\n"
            index_text += row + "\n"
            atomic_write(index_path, index_text)

            # Update knowledge/log.md
            log_path = KNOWLEDGE_DIR / "log.md"
            log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            timestamp = datetime.datetime.now(timezone.utc).isoformat()
            log_entry = f"## [{timestamp}] query | \"{question}\"\n"
            if consulted:
                log_entry += "- Consulted: " + ", ".join(f"[[{c}]]" for c in consulted) + "\n"
            log_entry += f"- Filed to: [[qa/{today}-{slug}]]\n"
            if not log_text.endswith("\n"):
                log_text += "\n"
            log_text += log_entry + "\n"
            atomic_write(log_path, log_text)
        finally:
            release_lock(KNOWLEDGE_DIR)

    return answer


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the Hermes Memory Compiler knowledge base.")
    parser.add_argument("question", nargs="+", help="Question to ask")
    parser.add_argument("--file-back", action="store_true", help="Persist answer to knowledge/qa/")
    args = parser.parse_args()

    question = " ".join(args.question)
    answer = run_query(question, file_back=args.file_back)
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
