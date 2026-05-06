# Hermes Memory Compiler

For full technical reference, see [AGENTS.md](AGENTS.md).

A personal knowledge base that compiles your AI conversations into a structured, queryable archive. Instead of manually organizing notes, you talk to the LLM and it extracts concepts, connections, and lessons automatically.

## The Compiler Analogy

```
Conversation → Hooks (on_post_llm_call) → flush.py → daily/ → compile.py → knowledge/ → on_pre_llm_call → Next Session
```

- `daily/` = source code (your raw conversations — append-only, never edited)
- LLM = compiler (extracts, organizes, and cross-references knowledge)
- `knowledge/` = executable (structured, queryable articles, connections, and Q&A)
- lint = test suite (health checks for broken links, orphans, contradictions)
- queries = runtime (the knowledge base answering questions in real time)

> **Unified KB:** The knowledge base at `knowledge/` (or wherever `wiki_path` points in `config.yaml`) is shared between Hermes and Claude Code. Articles are tagged by source (`[Hermes]` or `[ClaudeCode]`). A compilation lock file (`.compile.lock`) prevents concurrent writes.

## What is this?

The Memory Compiler treats your daily conversation logs as **source code** and uses an LLM as a **compiler** to produce structured knowledge articles:

- `daily/` → raw conversation logs (append-only)
- `knowledge/` → compiled articles, connections, and Q&A
- `scripts/` → compiler, linter, query engine, and flush daemon
- `hermes_memory_compiler/` → Hermes CLI plugin for lifecycle hooks and commands

## Installation

1. Clone the repository:

```bash
git clone <repo-url> HermesMemoryCompiler
cd HermesMemoryCompiler
```

2. Ask the user where their vault (knowledge base target) lives. Then copy the HMC into it:

```bash
cp -r /path/to/HermesMemoryCompiler /path/to/your/vault/
```

3. Install the Hermes plugin via symlink:

```bash
cd /path/to/your/vault/HermesMemoryCompiler
ln -s $(pwd)/hermes_memory_compiler ~/.hermes/plugins/hermes-memory-compiler
```

4. Enable the plugin:

```bash
hermes plugins enable hermes-memory-compiler
```

5. Update `config.yaml` — set `wiki_path` to the user's actual knowledge directory if it differs from the default `knowledge`.

## Configuration

Edit `config.yaml` at the project root:

```yaml
ollama:
  base_url: "http://localhost:11434/v1"
  model: "kimi-k2.6:cloud"
flush:
  temperature: 0.2
  max_tokens: 2048
  min_turns_before_flush: 3
  max_messages_per_flush: 50
compiler:
  temperature: 0.3
  max_tokens: 4096
  max_turns: 30
  context_article_threshold: 150
  system_prompt: "compiler"
query:
  temperature: 0.2
  max_tokens: 2048
lint:
  structural_only: false
  contradiction_temperature: 0.1
  contradiction_max_tokens: 2048
plugin:
  auto_flush: true
  auto_compile_hour: 18
  wiki_path: "knowledge"
  marker_dir: "~/.hermes/plugins/hermes-memory-compiler/markers"
  max_context_chars: 20000
  max_log_lines: 30
```

Adjust the Ollama URL and model to match your local setup.

## Cron Setup

Run the included helper to see the recommended crontab lines:

```bash
bash scripts/install-cron.sh
```

Then copy-paste the output into your crontab (`crontab -e`):

```
# Flush every 30 minutes
*/30 * * * * cd /full/path/to/HermesMemoryCompiler && python scripts/flush.py --all >> scripts/flush.log 2>&1
# Compile daily at 6 PM
0 18 * * * cd /full/path/to/HermesMemoryCompiler && python scripts/compile.py >> scripts/compile.log 2>&1
```

**Note:** When `flush.py` runs after `auto_compile_hour` (default 18:00), it automatically
invokes `compile.py` if the daily log has changed. You can disable this by setting
`auto_compile_hour` to a value > 23 in `config.yaml`.

## Usage

### CLI commands

```bash
# Compile today's (or all) daily logs into knowledge articles
hermes kb compile

# Lint the knowledge base for broken links, orphans, stale sources, and contradictions
hermes kb lint

# Query the knowledge base
hermes kb query "How do I handle auth redirects?"

# Flush pending session markers into daily logs
hermes kb flush

# Show current status (article count, last compile, last lint, active markers)
hermes kb status
```

### In-session slash commands

While chatting with Hermes, you can run knowledge-base operations directly:

```
/kbq How do I handle auth redirects?
/mcompile --dry-run
/mlint --structural-only
/mquery --file-back How do I handle auth redirects?
```

`/kbq` runs the same query engine as `hermes kb query` and returns the answer inline.
`/mcompile`, `/mlint`, and `/mquery` mirror their CLI counterparts.

## Directory Structure

```
.
├── daily/                  # Raw conversation logs
├── knowledge/
│   ├── index.md            # Master catalog of all articles
│   ├── log.md              # Append-only build log
│   ├── concepts/           # Atomic knowledge articles
│   ├── connections/        # Cross-cutting synthesis
│   └── qa/                 # Filed query answers
├── scripts/
│   ├── compile.py          # Compilation engine
│   ├── lint.py             # Structural & semantic health checks
│   ├── query.py            # Index-guided Q&A
│   ├── flush.py            # Session-to-daily flush daemon
│   ├── config.py           # Config loader & Ollama client
│   ├── utils.py            # Shared helpers
│   ├── state.json          # Compilation tracking (gitignored)
│   └── install-cron.sh     # Cron helper
├── hermes_memory_compiler/          # Hermes plugin package
│   ├── __init__.py         # CLI & slash command registration
│   ├── hooks.py            # Lifecycle hooks
│   ├── lock.py             # Cross-agent compilation lock
│   └── marker.py           # Session marker I/O
├── config.yaml             # User configuration
└── pyproject.toml          # Python dependencies
```

## Why No RAG?

> At personal knowledge base scale (50–500 articles), the LLM reading a structured index outperforms cosine similarity.
> — Andrej Karpathy

Embeddings find similar *words*; the LLM finds relevant *concepts*. When you ask "How do I handle auth redirects?", a vector search might return articles that mention "redirect" and "authentication." The LLM reading `knowledge/index.md` understands you are asking about **session flow**, **middleware patterns**, and **OAuth callbacks** — and selects the precise 3–10 articles that actually answer your question.

RAG shines at massive scale (10,000+ documents). For a personal knowledge base compiled from your own conversations, structured indexing plus LLM reasoning is simpler, cheaper, and more accurate.

## License

MIT


## Automatic Session Flush

When a Hermes session ends (quit, `/new`, or gateway GC), the Memory Compiler
plugin automatically flushes the conversation to the daily log via the
`on_session_finalize` hook. No manual action is required.

To disable automatic flush, set `auto_flush: false` in `config.yaml`:

```yaml
plugin:
  auto_flush: false
```
