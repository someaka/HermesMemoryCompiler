# AGENTS.md - Personal Knowledge Base Schema

> Adapted from [Andrej Karpathy's LLM Knowledge Base](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) architecture.
> Instead of ingesting external articles, this system compiles knowledge from your own AI conversations.

## The Compiler Analogy

```
daily/          = source code    (your conversations - the raw material)
LLM             = compiler       (extracts and organizes knowledge)
knowledge/      = executable     (structured, queryable knowledge base)
lint            = test suite     (health checks for consistency)
queries         = runtime        (using the knowledge)
```

> **Unified KB:** The knowledge base at `/home/d/Desktop/agenda/ObsidianVault/knowledge/` is shared between Hermes and Claude Code. Articles are tagged by source: `[Hermes]` for Hermes-compiled articles, `[ClaudeCode]` for Claude Code articles. Both agents must acquire the compilation lock (`.compile.lock`) before writing to the KB to prevent race conditions.

You don't manually organize your knowledge. You have conversations, and the LLM handles the synthesis, cross-referencing, and maintenance.

---

## Architecture

### Layer 1: `daily/` - Conversation Logs (Immutable Source)

Daily logs capture what happened in your AI coding sessions. These are the "raw sources" - append-only, never edited after the fact.

```
daily/
├── 2026-04-01.md
├── 2026-04-02.md
└── ...
```

Each file follows this format:

```markdown
# Daily Log: YYYY-MM-DD

## Sessions

### Session (HH:MM) - Brief Title

**Context:** What the user was working on.

**Key Exchanges:**
- User asked about X, assistant explained Y
- Decided to use Z approach because...
- Discovered that W doesn't work when...

**Decisions Made:**
- Chose library X over Y because...
- Architecture: went with pattern Z

**Lessons Learned:**
- Always do X before Y to avoid...
- The gotcha with Z is that...

**Action Items:**
- [ ] Follow up on X
- [ ] Refactor Y when time permits
```

### Layer 2: `knowledge/` - Compiled Knowledge (LLM-Owned)

The LLM owns this directory entirely. Humans read it but rarely edit it directly.

```
knowledge/
├── index.md              # Master catalog - every article with one-line summary
├── log.md                # Append-only chronological build log
├── concepts/             # Atomic knowledge articles
├── connections/          # Cross-cutting insights linking 2+ concepts
└── qa/                   # Filed query answers (compounding knowledge)
```

### Layer 3: This File (AGENTS.md)

The schema that tells the LLM how to compile and maintain the knowledge base. This is the "compiler specification."

---

## Structural Files

### `knowledge/index.md` - Master Catalog

A table listing every knowledge article. This is the primary retrieval mechanism - the LLM reads this FIRST when answering any query, then selects relevant articles to read in full.

Format:

```markdown
# Knowledge Base Index

| Article | Summary | Compiled From | Updated |
|---------|---------|---------------|---------|
| [[concepts/supabase-auth]] | Row-level security patterns and JWT gotchas | daily/2026-04-02.md | 2026-04-02 |
| [[connections/auth-and-webhooks]] | Token verification patterns shared across Supabase auth and Stripe webhooks | daily/2026-04-02.md, daily/2026-04-04.md | 2026-04-04 |
```

### `knowledge/log.md` - Build Log

Append-only chronological record of every compile, query, and lint operation.

Format:

```markdown
# Build Log

## [2026-04-01T14:30:00] compile | Daily Log 2026-04-01
- Source: daily/2026-04-01.md
- Articles created: [[concepts/nextjs-project-structure]], [[concepts/tailwind-setup]]
- Articles updated: (none)

## [2026-04-02T09:00:00] query | "How do I handle auth redirects?"
- Consulted: [[concepts/supabase-auth]], [[concepts/nextjs-middleware]]
- Filed to: [[qa/auth-redirect-handling]]
```

---

## Article Formats

### Concept Articles (`knowledge/concepts/`)

One article per atomic piece of knowledge. These are facts, patterns, decisions, preferences, and lessons extracted from your conversations.

```markdown
---
title: "Concept Name"
aliases: [alternate-name, abbreviation]
tags: [domain, topic, Hermes]
sources:
  - "daily/2026-04-01.md"
  - "daily/2026-04-03.md"
created: 2026-04-01
updated: 2026-04-03
---

# Concept Name

[2-4 sentence core explanation]

## Key Points

- [Bullet points, each self-contained]

## Details

[Deeper explanation, encyclopedia-style paragraphs]

## Related Concepts

- [[concepts/related-concept]] - How it connects

## Sources

- [[daily/2026-04-01.md]] - Initial discovery during project setup
- [[daily/2026-04-03.md]] - Updated after debugging session
```

### Connection Articles (`knowledge/connections/`)

Cross-cutting synthesis linking 2+ concepts. Created when a conversation reveals a non-obvious relationship.

```markdown
---
title: "Connection: X and Y"
tags: [cross-cutting, Hermes]
connects:
  - "concepts/concept-x"
  - "concepts/concept-y"
sources:
  - "daily/2026-04-04.md"
created: 2026-04-04
updated: 2026-04-04
---

# Connection: X and Y

## The Connection

[What links these concepts]

## Key Insight

[The non-obvious relationship discovered]

## Evidence

[Specific examples from conversations]

## Related Concepts

- [[concepts/concept-x]]
- [[concepts/concept-y]]
```

### Q&A Articles (`knowledge/qa/`)

Filed answers from queries. Every complex question answered by the system can be permanently stored, making future queries smarter.

```markdown
---
title: "Q: Original Question"
tags: [qa, Hermes]
question: "The exact question asked"
consulted:
  - "concepts/article-1"
  - "concepts/article-2"
filed: 2026-04-05
---

# Q: Original Question

## Answer

[The synthesized answer with [[wikilinks]] to sources]

## Sources Consulted

- [[concepts/article-1]] - Relevant because...
- [[concepts/article-2]] - Provided context on...

## Follow-Up Questions

- What about edge case X?
- How does this change if Y?
```

---

## Core Operations

### 1. Compile (daily/ -> knowledge/)

When processing a daily log:

1. Read the daily log file
2. Read `knowledge/index.md` to understand current knowledge state
3. Read existing articles that may need updating
4. For each piece of knowledge found in the log:
   - If an existing concept article covers this topic: UPDATE it with new information, add the daily log as a source
   - If it's a new topic: CREATE a new `concepts/` article
5. If the log reveals a non-obvious connection between 2+ existing concepts: CREATE a `connections/` article
6. UPDATE `knowledge/index.md` with new/modified entries
7. APPEND to `knowledge/log.md`

**Important guidelines:**
- A single daily log may touch 3-10 knowledge articles
- Prefer updating existing articles over creating near-duplicates
- Use Obsidian-style `[[wikilinks]]` with full relative paths from knowledge/
- Write in encyclopedia style - factual, concise, self-contained
- Every article must have YAML frontmatter
- Every article must link back to its source daily logs

### 2. Query (Ask the Knowledge Base)

1. Read `knowledge/index.md` (the master catalog)
2. Based on the question, identify 3-10 relevant articles from the index
3. Read those articles in full
4. Synthesize an answer with `[[wikilink]]` citations
5. If `--file-back` is specified: create a `knowledge/qa/` article and update index.md and log.md

**Why this works without RAG:** At personal knowledge base scale (50-500 articles), the LLM reading a structured index outperforms cosine similarity. The LLM understands what the question is really asking and selects pages accordingly. Embeddings find similar words; the LLM finds relevant concepts.

### 3. Lint (Health Checks)

Seven checks, run periodically:

1. **Broken links** - `[[wikilinks]]` pointing to non-existent articles
2. **Orphan pages** - Articles with zero inbound links from other articles
3. **Orphan sources** - Daily logs that haven't been compiled yet
4. **Stale articles** - Source daily log changed since article was last compiled
5. **Contradictions** - Conflicting claims across articles (requires LLM judgment)
6. **Missing backlinks** - A links to B but B doesn't link back to A
7. **Sparse articles** - Below 200 words, likely incomplete

Output: a markdown report with severity levels (error, warning, suggestion).

---

## Hermes Plugin System

The `hermes_memory_compiler/` package integrates the Memory Compiler with the Hermes CLI.

### Hook Lifecycle

| Hook | When It Fires | What It Does |
|------|---------------|--------------|
| `on_pre_llm_call` | Before every LLM call, first turn only | Injects KB context (`knowledge/index.md` + today's daily log last N lines) into the user message |
| `on_post_llm_call` | After every successful assistant response | Writes a marker JSON file recording message count and timestamp |
| `on_session_finalize` | At session boundary (quit, `/new`, gateway GC) | Flushes conversation to daily log via `flush.py`, then deletes marker |

### Marker Files

Small JSON files in `~/.hermes/plugins/hermes-memory-compiler/markers/<session_id>.json` tracking:
- `message_count` — how many messages were in the session at last flush
- `last_flush_timestamp` — ISO 8601 timestamp
- `flush_count` — how many times this session has been flushed

Markers are atomically written (temp-then-rename) and cleaned up after `on_session_finalize`.

### Automatic Flush

Enabled by default (`auto_flush: true` in `config.yaml`). When a session ends, the plugin:
1. Checks if the marker shows new messages since last flush
2. Calls `scripts/flush.py --session <ID>` via subprocess
3. Deletes the marker regardless of flush success/failure

To disable: set `plugin.auto_flush: false` in `config.yaml`.

### CLI Commands

Registered via `hermes_memory_compiler/__init__.py`:

| Command | Action |
|---------|--------|
| `hermes kb compile` | Runs `scripts/compile.py` |
| `hermes kb lint` | Runs `scripts/lint.py` |
| `hermes kb query "..."` | Runs `scripts/query.py "..."` |
| `hermes kb flush` | Runs `scripts/flush.py --all` |
| `hermes kb status` | Shows article count, last compile, last lint, active markers |

### Slash Command

| Command | Action |
|---------|--------|
| `/kbq <question>` | In-session query against the knowledge base |

### Config Keys

Under the `plugin:` section in `config.yaml`:

| Key | Default | Description |
|-----|---------|-------------|
| `wiki_path` | `<project>/knowledge` | Path to the compiled knowledge directory |
| `marker_dir` | `~/.hermes/plugins/hermes-memory-compiler/markers` | Where session marker JSON files are stored |
| `max_context_chars` | `20000` | Maximum characters of KB context injected per turn |
| `max_log_lines` | `30` | Number of trailing lines from today's daily log to include |
| `auto_flush` | `true` | Whether to automatically flush sessions at session boundary |

---

## Conventions

- **Wikilinks:** Use Obsidian-style `[[path/to/article]]` without `.md` extension
- **Writing style:** Encyclopedia-style, factual, third-person where appropriate
- **Dates:** ISO 8601 (YYYY-MM-DD for dates, full ISO for timestamps in log.md)
- **File naming:** lowercase, hyphens for spaces (e.g., `supabase-row-level-security.md`)
- **Frontmatter:** Every article must have YAML frontmatter with at minimum: title, sources, created, updated
- **Sources:** Always link back to the daily log(s) that contributed to an article

---

## Full Project Structure

```
hermes-memory-compiler/
├── config.yaml              # Ollama and compiler configuration
├── AGENTS.md                # This file - schema + full technical reference
├── README.md                # Quick start and overview
├── pyproject.toml           # Dependencies
├── daily/                   # "Source code" - conversation logs (immutable)
│   └── ...
└── knowledge/               # "Executable" - compiled knowledge (LLM-owned)
    ├── index.md             #   Master catalog - THE retrieval mechanism
    ├── log.md               #   Append-only build log
    ├── concepts/            #   Atomic knowledge articles
    ├── connections/         #   Cross-cutting insights linking 2+ concepts
    └── qa/                  #   Filed query answers (compounding knowledge)
```

---

## Script Details

### compile.py - The Compiler

Uses the Ollama OpenAI-compatible endpoint (`/v1/chat/completions`):

- Builds a prompt with: AGENTS.md schema, current index, all existing articles, and the daily log
- The LLM reads the daily log, decides what concepts to extract, and returns structured edits
- Incremental: tracks SHA-256 hashes of daily logs in `state.json`, skips unchanged files
- Atomic writes for all state mutations

**CLI:**
```bash
python scripts/compile.py              # compile new/changed only
python scripts/compile.py --all        # force recompile everything
python scripts/compile.py --file daily/2026-04-01.md
python scripts/compile.py --dry-run
```

### query.py - Index-Guided Retrieval

Loads the knowledge base index into context, asks the LLM to select relevant articles, reads them, then synthesizes an answer. No RAG.

**CLI:**
```bash
python scripts/query.py "What auth patterns do I use?"
python scripts/query.py "What's my error handling strategy?" --file-back
```

With `--file-back`, creates a Q&A article in `knowledge/qa/` and updates the index and log. This is the compounding loop - every question makes the KB smarter.

### lint.py - Health Checks

Seven checks:

| Check | Type | Catches |
|-------|------|---------|
| Broken links | Structural | `[[wikilinks]]` to non-existent articles |
| Orphan pages | Structural | Articles with zero inbound links |
| Orphan sources | Structural | Daily logs not yet compiled |
| Stale articles | Structural | Source logs changed since compilation |
| Missing backlinks | Structural | A links to B but B doesn't link back to A |
| Sparse articles | Structural | Under 200 words |
| Contradictions | LLM | Conflicting claims across articles |

**CLI:**
```bash
python scripts/lint.py                    # all checks
python scripts/lint.py --structural-only  # skip LLM check (free)
```

Reports saved to `reports/lint-YYYY-MM-DD.md`.

---

## State Tracking

`scripts/state.json` tracks:
- `ingested` - map of daily log filenames to SHA-256 hashes, compilation timestamps
- `query_count` - total queries run
- `last_lint` - timestamp of most recent lint

`scripts/last-flush.json` tracks flush deduplication (session_id + timestamp).

Both are gitignored and regenerated automatically.

---

## Dependencies

`pyproject.toml` (at project root):
- `pyyaml>=6.0` - YAML config parsing
- Python 3.12+, managed by `uv` or `pip`

All LLM calls go through Ollama at `localhost:11434/v1`. No API key needed.

---

## Costs

All operations use local Ollama inference. Costs are zero monetary but proportional to context size:

| Operation | Approximate Tokens |
|-----------|--------------------|
| Compile one daily log | 4K-8K |
| Query (no file-back) | 2K-4K |
| Query (with file-back) | 3K-5K |
| Full lint (with contradictions) | 2K-4K |
