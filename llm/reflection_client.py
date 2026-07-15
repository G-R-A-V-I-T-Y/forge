"""llm/reflection_client.py — raw-text completion transport for the reflection pipeline.

The standard decision path (llm/model_chain.py → llama_server_client.py → ollama_client.py)
validates every LLM response as a trade decision (``action ∈ enter/wait/close``) and
coerces it through ``_extract_json`` → ``_is_valid_decision``.  Scheduled reflections
(spec YAML, thesis markdown, research notes) are *not* trade decisions — every response
is rejected "by construction", silently killing the entire reflection pipeline.

This module provides a dedicated ``complete(system_prompt, user_prompt)`` that:

* Returns the model's **raw text** as a plain string — no JSON extraction, no schema
  validation, no coercion.
* Supports the same backend vocabulary as model_chain (``llama_server``, ``opencode``).
* Defaults to ``llama_server`` with reasoning **ON** — reflections are rare, so the
  latency is acceptable and the thinking quality is worth it.
* Reads ``reflection_model`` from the settings DB at call time so Settings → Save &
  Apply takes effect in the next reflection cycle.
* Logs a model-id + prompt-hash fingerprint on every call for observability.
* Never raises — returns ``""`` on any failure (timeout, HTTP error, empty content).

Design notes
------------
* The ``_extract_content_from_response`` helper extracts the ``choices[0].message.content``
  string from the OpenAI-compatible response envelope and returns it verbatim.  It does NOT
  attempt to parse the content as JSON — the caller gets exactly what the model produced.
* The async inner function ``_complete_async`` handles the HTTP call; ``complete()`` is the
  synchronous wrapper that reuses the ``_run_coroutine_sync`` pattern from model_chain so
  it can be called from synchronous reflection schedulers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from typing import Any, Coroutine, TypeVar

import httpx

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# Settings DB constants (mirrored from llm/model_chain.py)
# ---------------------------------------------------------------------------
_SETTINGS_DB_PATH = "data/forge.db"
_DEFAULT_LLAMA_PORT = 8080

# Default timeout — generous because reflections are rare and may use
# reasoning mode (qwen3.6 with thinking: 160-290 s measured).
_DEFAULT_TIMEOUT_SECS = 900


# ---------------------------------------------------------------------------
# Sync-over-async runner (identical pattern to llm/model_chain.py)
# ---------------------------------------------------------------------------
def _run_coroutine_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run an async coroutine to completion from synchronous code.

    Avoids ``asyncio.run()`` when an event loop is already running (the common
    case inside agents/agent_runner.py's ``asyncio.run(_run_once(...))``) by
    spawning a dedicated thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result["value"]


# ---------------------------------------------------------------------------
# Prompt fingerprinting (for log lines)
# ---------------------------------------------------------------------------
def _prompt_hash(system_prompt: str, user_prompt: str) -> str:
    """Return a short, stable SHA-256 prefix for prompt observability."""
    blob = f"{system_prompt}\n\n{user_prompt}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
def _read_port() -> int:
    """Read the llama-server port from the settings DB."""
    try:
        import sqlite3

        conn = sqlite3.connect(_SETTINGS_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'llama_server_port'"
        ).fetchone()
        conn.close()
        if row is not None:
            return int(json.loads(row["value"]))
    except Exception:
        pass
    return _DEFAULT_LLAMA_PORT


def _read_reflection_model() -> str:
    """Read the ``reflection_model`` setting, falling back to ``llama_server``.

    Returns one of: ``"llama_server"``, ``"opencode"``, or a specific model id
    string that would be routed to an opencode subprocess.
    """
    try:
        import sqlite3

        conn = sqlite3.connect(_SETTINGS_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'reflection_model'"
        ).fetchone()
        conn.close()
        if row is not None:
            return json.loads(row["value"])
    except Exception:
        pass
    return "llama_server"


# ---------------------------------------------------------------------------
# Response extraction — raw text, NOT JSON
# ---------------------------------------------------------------------------
def _extract_content_from_response(body: dict) -> str | None:
    """Extract the verbatim text content from an OpenAI-compatible response body.

    This deliberately does **not** attempt JSON extraction or schema validation.
    The caller receives exactly what the model produced — YAML, markdown, prose,
    or any other free-form text.

    Returns the content string on success, ``None`` if the response shape is
    unexpected or the content is empty.
    """
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning(
            "reflection_client: unexpected response shape: %.200s", body
        )
        return None

    if not content:
        logger.warning("reflection_client: model returned empty content")
        return None

    return content


# ---------------------------------------------------------------------------
# Async backends
# ---------------------------------------------------------------------------
async def _complete_llama_server(
    system_prompt: str,
    user_prompt: str,
    port: int,
    timeout_s: int,
) -> str:
    """POST to llama-server's OpenAI-compat endpoint and return raw text.

    Unlike llm/llama_server_client.py's ``decide()``, this function:
    * Does NOT pass the response through ``_extract_json``.
    * Does NOT validate the content against any decision schema.
    * Returns the model's raw text output as-is.
    * Uses reasoning ON (no ``--reasoning off`` gate) because reflections are
      rare and benefit from extended thinking.
    """
    url = f"http://localhost:{port}/v1/chat/completions"
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 40,
        # Reflections are rare and latency-tolerant — enable thinking for
        # higher-quality reasoning on research/spec generation.
        "think": True,
    }

    try:
        async with httpx.AsyncClient(timeout=float(timeout_s)) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except httpx.TimeoutException:
        logger.warning(
            "reflection_client: llama-server timed out after %ds", timeout_s
        )
        return ""
    except Exception as exc:
        logger.error("reflection_client: llama-server request failed: %s", exc)
        return ""

    content = _extract_content_from_response(body)
    return content if content is not None else ""


async def _complete_opencode(
    _system_prompt: str,
    _user_prompt: str,
    _model_id: str,
    _timeout_s: int,
) -> str:
    """Stub for opencode-routed reflection backend.

    Currently returns ``""`` — a full implementation would spawn an
    ``opencode run`` subprocess similar to model_chain._run_opencode_tier()
    but without JSON extraction / decision validation.  Kept as a stub so
    the settings vocabulary ("opencode" as a reflection_model value) is
    accepted without error.
    """
    logger.info(
        "reflection_client: opencode backend is a stub; returning empty"
    )
    return ""


# ---------------------------------------------------------------------------
# Dispatching
# ---------------------------------------------------------------------------
async def _complete_async(
    system_prompt: str,
    user_prompt: str,
    timeout_s: int,
) -> str:
    """Dispatch to the appropriate backend based on ``reflection_model``."""
    backend = _read_reflection_model_effective()

    logger.info(
        "reflection_client: backend=%s prompt_hash=%s",
        backend,
        _prompt_hash(system_prompt, user_prompt),
    )

    if backend == "llama_server":
        port = _read_port()
        return await _complete_llama_server(
            system_prompt, user_prompt, port, timeout_s
        )

    if backend == "opencode":
        return await _complete_opencode(
            system_prompt, user_prompt, backend, timeout_s
        )

    # Any other string is treated as an opencode model id.
    logger.info(
        "reflection_client: treating %r as opencode model id (stub)", backend
    )
    return await _complete_opencode(
        system_prompt, user_prompt, backend, timeout_s
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def complete(
    system_prompt: str,
    user_prompt: str,
    timeout_s: int = _DEFAULT_TIMEOUT_SECS,
    settings: dict | None = None,
) -> str:
    """Return the model's raw text response for a reflection prompt.

    This is the **sole public entry point** of the module.  It is synchronous,
    matching the calling convention of the reflection scheduler (which runs
    synchronously from the heartbeat cycle).

    Args:
        system_prompt: The system-level instruction for the reflection task.
        user_prompt: The user-facing prompt (e.g. spec template, thesis query).
        timeout_s: Maximum seconds to wait for the LLM response (default 900).
        settings: Optional settings dict — reserved for future use where the
            caller may pre-load settings to avoid a DB read per call.  When
            provided, ``settings.get("reflection_model")`` overrides the DB
            lookup.  When ``None`` (the default), the DB is queried.

    Returns:
        The model's raw text output, or ``""`` on any failure (timeout, HTTP
        error, empty response, backend unavailable).  Never raises.

    Examples::

        # Typical reflection call from the scheduler
        raw = complete(
            system_prompt="You are a quantitative research analyst...",
            user_prompt="Generate a Q3 thesis for BTC based on...",
        )
        if raw:
            spec = yaml.safe_load(raw)  # caller decides how to parse
    """
    # If settings dict is provided with a reflection_model, write it into a
    # temporary override so _complete_async picks it up without a DB round-trip.
    # (This is a future-proofing hook; today the scheduler passes None.)
    has_override = settings and "reflection_model" in settings
    if has_override:
        _override_reflection_model(settings["reflection_model"])

    try:
        return _run_coroutine_sync(
            _complete_async(system_prompt, user_prompt, timeout_s)
        )
    except Exception as exc:
        logger.error("reflection_client: unexpected error: %s", exc)
        return ""
    finally:
        if has_override:
            global _reflection_model_override  # noqa: PLW0603
            _reflection_model_override = None


# ---------------------------------------------------------------------------
# Settings override (for caller-provided settings dict)
# ---------------------------------------------------------------------------
_reflection_model_override: str | None = None


def _override_reflection_model(model: str) -> None:
    """Temporarily override the reflection model (used when settings dict is provided)."""
    global _reflection_model_override  # noqa: PLW0603
    _reflection_model_override = model


def _read_reflection_model_effective() -> str:
    """Read reflection model with override support (replaces _read_reflection_model in dispatch)."""
    if _reflection_model_override is not None:
        return _reflection_model_override
    return _read_reflection_model()
