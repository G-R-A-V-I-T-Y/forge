"""Async HTTP client for the forge-managed llama-server (OpenAI-compat API).

The server is started by forge.py via llm/llama_server.py and exposes an
OpenAI-compatible endpoint at http://localhost:{port}/v1/chat/completions.
Because --reasoning off is set at server startup, every request gets a
fast (148-282 token) non-thinking response — no per-request think flag
needed.
"""
from __future__ import annotations

import logging

import httpx

from llm.ollama_client import _extract_json

logger = logging.getLogger(__name__)

# Timeout covers the full request including prefill + decode.
# With thinking off, 60s is generous (empirical: ~12-20s per decision).
TIMEOUT_SECS = 60.0

_DEFAULT_PORT = 8080


async def decide(
    system_prompt: str,
    decision_prompt: str,
    port: int = _DEFAULT_PORT,
) -> dict | None:
    """POST to llama-server's OpenAI-compat endpoint and extract a decision.

    Returns a parsed JSON dict on success, None on timeout or parse failure.
    """
    url = f"http://localhost:{port}/v1/chat/completions"
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": decision_prompt},
        ],
        "stream": False,
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 40,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECS) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except httpx.TimeoutException:
        logger.warning("llama-server request timed out after %ds", TIMEOUT_SECS)
        return None
    except Exception as exc:
        logger.error("llama-server request failed: %s", exc)
        return None

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("llama-server returned unexpected response shape: %.200s", body)
        return None

    if not content:
        logger.warning("llama-server returned empty content")
        return None

    return _extract_json(content)
