# Hermes Memory Compiler

A personal knowledge base that compiles your AI conversations into a structured, queryable archive. Instead of manually organizing notes, you talk to the LLM and it extracts concepts, connections, and lessons automatically.

## What is this?

The Memory Compiler treats your daily conversation logs as **source code** and uses an LLM as a **compiler** to produce structured knowledge articles:

- `daily/` → raw conversation logs (append-only)
- `knowledge/` → compiled articles, connections, and Q&A
- `scripts/` → compiler, linter, query engine, and flush daemon
- `hermes_plugin/` → Hermes CLI plugin for lifecycle hooks and commands

## Installation

1. Clone or copy this repository to your machine.
2. Install the Hermes plugin:

```bash
cd /path/to/hermes-memory-compiler
hermes plugins install .
# or create a symlink manually:
ln -s $(pwd)/hermes_plugin ~/.hermes/plugins/hermes-memory-compiler
```

3. Enable the plugin:

```bash
hermes plugins enable hermes-memory-compiler
```

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
  auto_compile_hour: 18
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
*/30 * * * * cd /full/path/to/hermes-memory-compiler && python scripts/flush.py --all >> scripts/flush.log 2>&1
# Compile daily at 6 PM
0 18 * * * cd /full/path/to/hermes-memory-compiler && python scripts/compile.py >> scripts/compile.log 2>&1
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

### In-session slash command

While chatting with Hermes, you can query the knowledge base directly:

```
/kbq How do I handle auth redirects?
```

This runs the same query engine as `hermes kb query` and returns the answer inline.

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
├── hermes_plugin/          # Hermes plugin package
│   ├── __init__.py         # CLI & slash command registration
│   ├── hooks.py            # Lifecycle hooks
│   └── marker.py           # Session marker I/O
├── config.yaml             # User configuration
└── pyproject.toml          # Python dependencies
```

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
