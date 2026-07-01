"""Unified LLM interface. config['llm_backend'] selects the backend."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def decide(system_prompt: str, decision_prompt: str, config: dict | None = None) -> dict:
    """Call the configured LLM backend and return a parsed decision dict.

    Never raises. Returns a dict with at minimum {"action": "wait", "reason": "..."}
    on failure.
    """
    backend = (config or {}).get("llm_backend", "stub")

    if backend == "stub":
        from llm.stub import decide as stub_decide
        return stub_decide(system_prompt, decision_prompt)

    if backend == "ollama":
        return _ollama_decide(system_prompt, decision_prompt, config)

    logger.warning("Unknown llm_backend %r, falling back to stub", backend)
    from llm.stub import decide as stub_decide
    return stub_decide(system_prompt, decision_prompt)


def _ollama_decide(system_prompt: str, decision_prompt: str, config: dict | None = None) -> dict:
    """Sync wrapper around async Ollama client."""
    import asyncio
    from llm.ollama_client import decide as ollama_decide

    try:
        result = asyncio.run(ollama_decide(system_prompt, decision_prompt, config))
        if result is not None:
            return result
    except Exception as exc:
        logger.error("Ollama decide error: %s", exc)

    return {"action": "wait", "reason": "LLM unavailable or timed out"}
