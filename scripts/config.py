"""Configuration loader and Ollama API client for Hermes Memory Compiler."""
from __future__ import annotations

import pathlib
from typing import Any

import requests
import yaml

# Path constants
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT_DIR / "daily"
KNOWLEDGE_DIR = ROOT_DIR / "knowledge"
SCRIPTS_DIR = ROOT_DIR / "scripts"
REPORTS_DIR = ROOT_DIR / "reports"
STATE_PATH = SCRIPTS_DIR / "state.json"
LAST_FLUSH_PATH = SCRIPTS_DIR / "last-flush.json"
CONFIG_PATH = ROOT_DIR / "config.yaml"


def _load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = _load_config()


def cfg(path: str, default: Any = None) -> Any:
    """Dot-path config lookup, e.g. cfg('ollama.model')."""
    node = CONFIG
    for part in path.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part, default)
        if node is None:
            return default
    return node


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
    base_url = cfg("ollama.base_url", "http://localhost:11434/v1").rstrip("/")
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
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        err_body = exc.response.text if exc.response else ""
        raise RuntimeError(f"Ollama HTTP {exc.response.status_code if exc.response else '?'}: {err_body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc
