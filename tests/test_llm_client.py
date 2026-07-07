"""Tests for llm/client.py — dispatch logic with stub backend."""
import json

import httpx
import pytest
import respx

from llm import client as llm_client
from llm.ollama_client import MODEL, OLLAMA_URL

SYSTEM = "You are a trader."
PROMPT = "What should we do with SOL?"


def test_stub_backend_returns_dict():
    result = llm_client.decide(SYSTEM, PROMPT, config={"llm_backend": "stub"})
    assert isinstance(result, dict)
    assert "action" in result


def test_default_backend_is_stub():
    result = llm_client.decide(SYSTEM, PROMPT)
    assert isinstance(result, dict)
    assert result["action"] == "enter"


def test_unknown_backend_falls_back_to_stub():
    result = llm_client.decide(SYSTEM, PROMPT, config={"llm_backend": "nonexistent"})
    assert isinstance(result, dict)
    assert "action" in result


def test_stub_config_none_uses_stub():
    result = llm_client.decide(SYSTEM, PROMPT, config=None)
    assert isinstance(result, dict)
    assert "action" in result


@respx.mock
def test_ollama_backend_passes_configured_model_through():
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    config = {"llm_backend": "ollama", "llm_model": "qwen3.6:35b_optimized"}
    result = llm_client.decide(SYSTEM, PROMPT, config=config)
    assert result == {"action": "wait", "reason": "ok"}
    sent_payload = json.loads(route.calls.last.request.content)
    assert sent_payload["model"] == "qwen3.6:35b_optimized"


@respx.mock
def test_ollama_backend_falls_back_to_default_model_without_llm_model():
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    config = {"llm_backend": "ollama"}
    result = llm_client.decide(SYSTEM, PROMPT, config=config)
    assert result == {"action": "wait", "reason": "ok"}
    sent_payload = json.loads(route.calls.last.request.content)
    assert sent_payload["model"] == MODEL == "qwen3.6:35b_optimized"


@pytest.mark.asyncio
@respx.mock
async def test_ollama_backend_works_when_called_from_a_running_event_loop():
    # Reproduces agents/agent_runner.py's real call chain: main() drives
    # everything via asyncio.run(_run_once(...)), and _run_once awaits
    # agents/decision_loop.py's run_decision(), which calls the sync
    # llm_fn() (wrapping model_chain.decide(), which calls this ollama
    # backend) directly — not awaited, but still executing while the loop
    # from asyncio.run() is running in this thread. _ollama_decide()'s old
    # implementation called asyncio.run() internally, which always raises
    # "asyncio.run() cannot be called from a running event loop" in this
    # exact situation — confirmed live: 2 of 10 agents in one real fleet
    # cycle got "no model available" this way, even though Ollama was
    # healthy and never even received the request.
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    config = {"llm_backend": "ollama", "llm_model": "qwen3.6:35b_optimized"}

    def call_from_sync_context():
        return llm_client.decide(SYSTEM, PROMPT, config=config)

    result = call_from_sync_context()
    assert result == {"action": "wait", "reason": "ok"}
