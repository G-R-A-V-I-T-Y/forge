"""Settings persistence via the existing `settings` SQLite table.

All values are stored as JSON strings. The module provides typed
read/write helpers and defines the defaults for all llama-server and
model-chain settings.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


# Default settings used when no DB value has been written yet.
DEFAULTS: dict[str, Any] = {
    "spawn_on_startup": False,
    "context_size": 24576,
    "batch_size": 2048,
    "ubatch_size": 1024,
    "threads": 6,
    "reasoning": False,
    "llama_server_binary": "",
    "llama_model_path": "",
    "llama_server_port": 8080,
    "gpu_layers": 99,
    "model_chain": [
        {
            "kind": "opencode",
            "model_id": "openrouter/anthropic/claude-sonnet-5",
            "variant": "low",
            "display_name": "Claude Sonnet 5 (low)",
        },
        {
            "kind": "opencode",
            "model_id": "opencode/deepseek-v4-flash-free",
            "variant": None,
            "display_name": "DeepSeek V4 Flash Free",
        },
        {
            "kind": "opencode",
            "model_id": "opencode/big-pickle",
            "variant": None,
            "display_name": "Big Pickle",
        },
        {
            "kind": "opencode",
            "model_id": "opencode/mimo-v2.5-free",
            "variant": None,
            "display_name": "MiMo V2.5 Free",
        },
        {
            "kind": "opencode",
            "model_id": "opencode/north-mini-code-free",
            "variant": None,
            "display_name": "North Mini Code Free",
        },
        {
            "kind": "opencode",
            "model_id": "opencode/nemotron-3-ultra-free",
            "variant": None,
            "display_name": "Nemotron 3 Ultra Free",
        },
        {
            "kind": "llama_server",
            "model_id": None,
            "variant": None,
            "display_name": "Local llama-server (Qwen3.6)",
        },
    ],
}

# Minimum safe context size given real prompt sizes (~10-11k tokens + answer headroom).
MIN_CONTEXT_SIZE = 12288


def load_all(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return all settings as a dict, merging DB values over DEFAULTS."""
    result = dict(DEFAULTS)
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        try:
            result[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            pass
    return result


def get(conn: sqlite3.Connection, key: str) -> Any:
    """Get one setting value, falling back to DEFAULTS[key] if unset."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key)
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return DEFAULTS.get(key)


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Upsert one setting key."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def save_all(conn: sqlite3.Connection, settings: dict[str, Any]) -> None:
    """Upsert all provided settings keys."""
    for key, value in settings.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
    conn.commit()


def validate_server_settings(settings: dict[str, Any]) -> list[str]:
    """Return a list of validation error strings, empty if all is valid."""
    errors: list[str] = []
    ctx = settings.get("context_size", DEFAULTS["context_size"])
    try:
        ctx = int(ctx)
        if ctx < MIN_CONTEXT_SIZE:
            errors.append(
                f"context_size must be at least {MIN_CONTEXT_SIZE} "
                f"(real prompts run ~10-11k tokens; got {ctx})"
            )
    except (TypeError, ValueError):
        errors.append("context_size must be an integer")

    for key in ("batch_size", "ubatch_size", "threads", "gpu_layers", "llama_server_port"):
        val = settings.get(key)
        if val is not None:
            try:
                v = int(val)
                if v < 1:
                    errors.append(f"{key} must be a positive integer")
            except (TypeError, ValueError):
                errors.append(f"{key} must be an integer")

    return errors
