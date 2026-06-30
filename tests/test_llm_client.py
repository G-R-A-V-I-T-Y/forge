"""Tests for llm/client.py — dispatch logic with stub backend."""
from llm import client as llm_client

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
