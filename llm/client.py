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
    """Sync wrapper around async Ollama client.

    Callers are almost never at module scope: agents/agent_runner.py's
    main() drives everything via asyncio.run(_run_once(...)), and this
    function is reached synchronously from inside that running loop (see
    agents/decision_loop.py's _call_llm_with_retry(), which calls llm_fn()
    directly rather than awaiting it). A plain `asyncio.run(...)` here
    would always raise "asyncio.run() cannot be called from a running
    event loop" in that situation — confirmed live, where it silently
    degraded to "no model available" for real trading decisions even
    though Ollama was healthy and never received the request. Running the
    coroutine on a dedicated thread with its own fresh event loop works
    whether or not the calling thread already has one running.
    """
    import asyncio
    import threading
    from llm.ollama_client import decide as ollama_decide

    box: dict = {}

    def runner() -> None:
        try:
            box["result"] = asyncio.run(ollama_decide(system_prompt, decision_prompt, config))
        except Exception as exc:  # noqa: BLE001 - reported via box, not raised across threads
            box["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()

    if "error" in box:
        logger.error("Ollama decide error: %s", box["error"])
    elif box.get("result") is not None:
        return box["result"]

    return {"action": "wait", "reason": "LLM unavailable or timed out"}
