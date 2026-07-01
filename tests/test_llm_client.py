"""Tests for llm/client.py — dispatch logic with stub backend."""
import json

import httpx
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
