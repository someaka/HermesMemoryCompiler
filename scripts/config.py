"""Configuration loader and Ollama API client for Hermes Memory Compiler."""
from __future__ import annotations

import pathlib
from typing import Any

import requests
import yaml

def _load_config(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config.yaml"
_CONFIG: dict[str, Any] | None = None


def _get_config() -> dict[str, Any]:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = _load_config(CONFIG_PATH)
    return _CONFIG


def cfg(path: str, default: Any = None) -> Any:
    """Dot-path config lookup, e.g. cfg('ollama.model')."""
    node = _get_config()
    for part in path.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part, default)
        if node is None:
            return default
    return node


# Path constants
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT_DIR / "daily"
_wiki = cfg("plugin.wiki_path", str(ROOT_DIR / "knowledge"))
KNOWLEDGE_DIR = ROOT_DIR / _wiki if not pathlib.Path(_wiki).is_absolute() else pathlib.Path(_wiki)
KNOWLEDGE_DIR = KNOWLEDGE_DIR.expanduser().resolve()
SCRIPTS_DIR = ROOT_DIR / "scripts"
REPORTS_DIR = ROOT_DIR / "reports"
STATE_PATH = SCRIPTS_DIR / "state.json"
LAST_FLUSH_PATH = SCRIPTS_DIR / "last-flush.json"


def ollama_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Call Ollama's OpenAI-compatible /v1/chat/completions endpoint.

    Returns the parsed JSON response. Raises RuntimeError on HTTP or
    parsing failures.
    """
    base_url = cfg("ollama.base_url", "http://localhost:11434/v1")
    base_url = base_url.rstrip("/")
    model = cfg("ollama.model", "kimi-k2.6:cloud")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if tools is not None:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=1800,  # 30 minutes — compilation may run for many minutes.
        )
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        err_body = exc.response.text if exc.response else ""
        raise RuntimeError(f"Ollama HTTP {exc.response.status_code if exc.response else '?'}: {err_body}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc
