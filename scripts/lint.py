#!/usr/bin/env python3
"""Structural and semantic lint checks for the Hermes Memory Compiler knowledge base.

Checks:
1. Broken links — wikilinks to non-existent articles
2. Orphan pages — articles with zero inbound links
3. Orphan sources — daily logs not in state.json
4. Stale articles — source daily log changed since compilation
5. Missing backlinks — A links to B but B doesn't link back
6. Sparse articles — below 200 words
7. Contradictions — conflicting claims across articles (LLM-powered)
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys
from typing import Any

from pathlib import Path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from scripts.config import DAILY_DIR, KNOWLEDGE_DIR, ROOT_DIR, cfg, ollama_completion
from hermes_memory_compiler.lock import LockHeldError, acquire_lock, release_lock
from scripts.utils import atomic_write, extract_wikilinks, hash_file, list_wiki_articles, load_state, now_iso, save_state

_WORD_RE = re.compile(r"\b\w+\b")

# Maximum characters to feed to the contradiction-check LLM.
# This is a pragmatic limit based on typical 2048-token output budgets
# and the need to keep prompt + response within context window.
MAX_CONTRADICTION_CHARS = 120_000


def _resolve_link(link: str) -> pathlib.Path | None:
    """Resolve a wikilink path to an actual filesystem path."""
    # Strip anchors
    clean = link.split("#")[0].strip()
    if not clean:
        return None

    # Try relative to knowledge/
    candidate = KNOWLEDGE_DIR / clean
    if candidate.exists():
        return candidate
    if not clean.endswith(".md"):
        candidate_md = KNOWLEDGE_DIR / (clean + ".md")
        if candidate_md.exists():
            return candidate_md

    # Try relative to root (e.g. daily/...)
    candidate_root = ROOT_DIR / clean
    if candidate_root.exists():
        return candidate_root
    if not clean.endswith(".md"):
        candidate_root_md = ROOT_DIR / (clean + ".md")
        if candidate_root_md.exists():
            return candidate_root_md

    return None


def _is_article(path: pathlib.Path) -> bool:
    resolved = path.resolve()
    if KNOWLEDGE_DIR in resolved.parents or resolved.parent == KNOWLEDGE_DIR:
        return True
    abs_path = path.absolute()
    return KNOWLEDGE_DIR in abs_path.parents or abs_path.parent == KNOWLEDGE_DIR


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def run_checks() -> dict[str, list[dict[str, Any]]]:
    issues: dict[str, list[dict[str, Any]]] = {
        "broken_links": [],
        "orphan_pages": [],
        "orphan_sources": [],
        "stale_articles": [],
        "missing_backlinks": [],
        "sparse_articles": [],
        "contradictions": [],
    }

    state = load_state()
    ingested: dict[str, Any] = state.get("ingested", {})

    # Gather all knowledge articles
    articles: list[pathlib.Path] = list(list_wiki_articles())
    article_set = {a.resolve() for a in articles}

    # Inbound link tracking: target path -> list of source paths
    inbound: dict[pathlib.Path, list[pathlib.Path]] = {a.resolve(): [] for a in articles}
    # Outbound links: source path -> list of target paths
    outbound: dict[pathlib.Path, list[pathlib.Path]] = {}

    # Source daily logs referenced by articles
    referenced_sources: set[str] = set()

    for article in articles:
        text = article.read_text(encoding="utf-8")
        links = extract_wikilinks(text)
        out: list[pathlib.Path] = []
        for link in links:
            target = _resolve_link(link)
            if target is None:
                issues["broken_links"].append({
                    "severity": "error",
                    "file": str(article.relative_to(ROOT_DIR)),
                    "link": link,
                    "message": f"Broken link: [[{link}]]",
                })
            else:
                out.append(target.resolve())
                if _is_article(target):
                    inbound.setdefault(target.resolve(), []).append(article.resolve())
                if str(target.relative_to(ROOT_DIR)).startswith("daily/"):
                    referenced_sources.add(str(target.relative_to(ROOT_DIR)))
        outbound[article.resolve()] = out

    # Orphan pages: knowledge articles with zero inbound links from other knowledge articles
    for article in articles:
        if not inbound.get(article.resolve()):
            # index.md and log.md are allowed to be orphans
            name = article.name
            if name not in ("index.md", "log.md"):
                issues["orphan_pages"].append({
                    "severity": "warning",
                    "file": str(article.relative_to(ROOT_DIR)),
                    "message": "Article has zero inbound links from other articles",
                })

    # Orphan sources: daily logs not present in state.json ingested map
    if DAILY_DIR.exists():
        for daily in sorted(DAILY_DIR.glob("*.md")):
            rel = str(daily.relative_to(ROOT_DIR))
            if rel not in ingested:
                issues["orphan_sources"].append({
                    "severity": "warning",
                    "file": rel,
                    "message": "Daily log has not been compiled yet (not in state.json)",
                })

    # Stale articles: source daily log changed since compilation (hash mismatch)
    for rel, meta in ingested.items():
        daily_path = ROOT_DIR / rel
        if not daily_path.exists():
            continue
        stored_hash = meta.get("hash", "")
        current_hash = hash_file(daily_path)
        if current_hash != stored_hash:
            # Find articles that list this daily as a source
            for article in articles:
                text = article.read_text(encoding="utf-8")
                if rel in text or rel.replace(".md", "") in text:
                    issues["stale_articles"].append({
                        "severity": "warning",
                        "file": str(article.relative_to(ROOT_DIR)),
                        "source": rel,
                        "message": f"Source {rel} changed since last compilation",
                    })

    # Missing backlinks
    for src, targets in outbound.items():
        src_path = pathlib.Path(src)
        if not _is_article(src_path):
            continue
        # Skip structural files that are expected to link out without backlinks
        if src_path.name in ("index.md", "log.md"):
            continue
        for tgt in targets:
            tgt_path = pathlib.Path(tgt)
            if not _is_article(tgt_path):
                continue
            # Check if tgt links back to src
            tgt_text = tgt_path.read_text(encoding="utf-8")
            src_rel = str(src_path.relative_to(KNOWLEDGE_DIR)).replace(".md", "")
            # A bit lenient: look for the src path without .md in tgt wikilinks
            if src_rel not in extract_wikilinks(tgt_text):
                issues["missing_backlinks"].append({
                    "severity": "suggestion",
                    "file": str(src_path.relative_to(ROOT_DIR)),
                    "target": str(tgt_path.relative_to(ROOT_DIR)),
                    "message": f"Links to {tgt_path.relative_to(ROOT_DIR)} but backlink is missing",
                })

    # Sparse articles
    for article in articles:
        if article.name in ("index.md", "log.md"):
            continue
        text = article.read_text(encoding="utf-8")
        # Remove YAML frontmatter for word count
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2]
        if _word_count(body) < 200:
            issues["sparse_articles"].append({
                "severity": "suggestion",
                "file": str(article.relative_to(ROOT_DIR)),
                "words": _word_count(body),
                "message": f"Article is sparse ({_word_count(body)} words, threshold 200)",
            })

    return issues


def format_report(issues: dict[str, list[dict[str, Any]]], structural_only: bool) -> str:
    lines = ["# Lint Report", ""]
    total = 0
    for category, items in issues.items():
        if structural_only and category == "contradictions":
            continue
        if not items:
            continue
        total += len(items)
        lines.append(f"## {category.replace('_', ' ').title()} ({len(items)})")
        for item in items:
            sev = item.get("severity", "info")
            file = item.get("file", "unknown")
            msg = item.get("message", "")
            lines.append(f"- [{sev.upper()}] `{file}` — {msg}")
        lines.append("")
    if total == 0:
        lines.append("No issues found.")
    else:
        lines.append(f"**Total issues:** {total}")
    return "\n".join(lines)


def _check_contradictions() -> list[dict[str, Any]]:
    """Ask an LLM to scan all knowledge articles for contradictory claims."""
    articles = list(list_wiki_articles())
    contents: list[str] = []
    for article in articles:
        if article.name in ("index.md", "log.md"):
            continue
        text = article.read_text(encoding="utf-8")
        rel = str(article.relative_to(KNOWLEDGE_DIR)).replace("\\", "/")
        contents.append(f"--- {rel} ---\n{text}")
    if not contents:
        return []

    full_text = "\n\n".join(contents)
    if len(full_text) > MAX_CONTRADICTION_CHARS:
        full_text = full_text[:MAX_CONTRADICTION_CHARS] + "\n\n[Additional articles truncated due to length]"
    prompt = (
        "You are a knowledge base auditor. Below are all knowledge base articles. "
        "Identify any pairs of claims that directly contradict each other.\n\n"
        f"{full_text}\n\n"
        "Return your findings as a JSON array of objects with keys: "
        "article_a, article_b, claim_a, claim_b. "
        "Use article paths relative to knowledge/ (e.g. concepts/example). "
        "If no contradictions, return []."
    )
    messages = [
        {"role": "system", "content": "You output only valid JSON arrays of objects."},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = ollama_completion(
            messages=messages,
            temperature=cfg("lint.contradiction_temperature", 0.2),
            max_tokens=cfg("lint.contradiction_max_tokens", 2048),
        )
        choices = resp.get("choices")
        if not choices:
            return [{
                "severity": "warning",
                "file": "contradiction-check",
                "message": "Ollama returned empty choices for contradiction check",
            }]
        content = choices[0].get("message", {}).get("content", "")
        # Try to parse the entire response first; fall back to regex extraction
        # because some models wrap the JSON in markdown fences or prose.
        stripped = content.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            data = json.loads(stripped)
            if isinstance(data, list):
                issues = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    issues.append({
                        "severity": "warning",
                        "file": f"{item.get('article_a', '?')} vs {item.get('article_b', '?')}",
                        "message": f"Contradiction: {item.get('claim_a', '?')} vs {item.get('claim_b', '?')}",
                    })
                return issues
        match = re.search(r"\[.*?\]", content, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                issues = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    issues.append({
                        "severity": "warning",
                        "file": f"{item.get('article_a', '?')} vs {item.get('article_b', '?')}",
                        "message": f"Contradiction: {item.get('claim_a', '?')} vs {item.get('claim_b', '?')}",
                    })
                return issues
    except (RuntimeError, json.JSONDecodeError) as exc:
        return [{
            "severity": "warning",
            "file": "contradiction-check",
            "message": f"Contradiction check failed: {exc}",
        }]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint the Hermes Memory Compiler knowledge base.")
    parser.add_argument("--structural-only", action="store_true", help="Skip LLM-powered checks (contradictions)")
    parser.add_argument("--output", type=str, default=None, help="Write report to file")
    args = parser.parse_args()

    try:
        acquire_lock(KNOWLEDGE_DIR, "hermes")
    except LockHeldError as exc:
        print(f"Error: Compilation lock held by {exc.agent_name} since {exc.timestamp} (pid {exc.pid})")
        return 2

    try:
        issues = run_checks()
        if not args.structural_only:
            issues["contradictions"] = _check_contradictions()

        report = format_report(issues, args.structural_only)

        if args.output:
            atomic_write(args.output, report)
        else:
            today = datetime.date.today().isoformat()
            default_path = ROOT_DIR / "reports" / f"lint-{today}.md"
            default_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(default_path, report)
            print(report)

        # Update last_lint timestamp in state
        state = load_state()
        state["last_lint"] = now_iso()
        save_state(state)

        # Return non-zero if any errors exist
        error_count = sum(
            1 for items in issues.values() for item in items if item.get("severity") == "error"
        )
        return 1 if error_count > 0 else 0
    finally:
        release_lock(KNOWLEDGE_DIR)


if __name__ == "__main__":
    sys.exit(main())
