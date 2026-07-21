"""Async Ollama client for local LLM inference."""
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
# qwen3.6:35b_optimized is a "thinking" model; a single unloaded
# trading-decision prompt on this hardware measured 121-194s end to end (see
# PR description for the manual verification run). This tier is also the
# last resort in llm/model_chain.py's fallback chain, and forge.py's fleet
# cycle spawns every agent concurrently (asyncio.gather) — when several
# agents fall through the upstream opencode tiers around the same time,
# their requests queue against this one local model instance, so a single
# request can take much longer than the unloaded baseline. 300s made real
# concurrent decisions time out and silently degrade to {"action": "wait",
# "reason": "LLM unavailable or timed out"} even though the Ollama server
# was up and healthy. 900s gives headroom for several queued requests.
TIMEOUT_SECS = 900.0
MODEL = "qwen3.6:35b_optimized"


async def decide(system_prompt: str, decision_prompt: str, config: dict | None = None) -> dict | None:
    """Call Ollama's /api/chat and extract a JSON trading decision.

    Args:
        system_prompt: The system prompt.
        decision_prompt: The decision prompt.
        config: Optional config dict; if it contains "llm_model", that value
            is used as the Ollama model name instead of the module default.

    Returns parsed JSON dict on success, None on timeout or parse failure.
    """
    model = (config or {}).get("llm_model") or MODEL
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": decision_prompt},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # qwen3.6 is a thinking model; without think:false every decision
        # burns 120-290s of reasoning tokens vs ~13s with it (measured live
        # 2026-07-13, 18.5k-token prompt) with no quality loss (CLAUDE.md
        # empirics). Overridable via config["llm_think"] for experiments.
        "think": bool((config or {}).get("llm_think", False)),
        "options": {"temperature": 0.7},
        # Default Ollama keep_alive is 5m -- the same cadence as forge.py's
        # heartbeat cycle, so a cycle that starts even a few seconds late
        # pays a full reload of this 36B model before it can start
        # inferring. 30m keeps it resident across several cycles' worth of
        # idle time between calls.
        "keep_alive": "30m",
    }
    start = time.monotonic()
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
    logger.info("Ollama request completed in %.1fs", time.monotonic() - start)

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
