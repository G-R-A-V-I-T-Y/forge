"""Async Ollama client for local LLM inference."""
import json
import logging

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
TIMEOUT_SECS = 30.0
MODEL = "qwen3:35b"


async def decide(system_prompt: str, decision_prompt: str) -> dict | None:
    """Call Ollama's /api/chat and extract a JSON trading decision.

    Returns parsed JSON dict on success, None on timeout or parse failure.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": decision_prompt},
    ]
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.7},
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECS) as client:
            resp = await client.post(OLLAMA_URL, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except httpx.TimeoutException:
        logger.warning("Ollama request timed out after %ds", TIMEOUT_SECS)
        return None
    except Exception as exc:
        logger.error("Ollama request failed: %s", exc)
        return None

    content = body.get("message", {}).get("content", "")
    if not content:
        logger.warning("Ollama returned empty content")
        return None

    return _extract_json(content)


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from LLM text output."""
    # Try parsing the whole output first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Look for a JSON block between ```json and ```
    import re
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Look for a bare { ... } block
    m = re.search(r"(\{.*\})", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    logger.warning("Could not extract JSON from Ollama output: %.200s", text)
    return None
