"""Tests for llm/ollama_client.py — all offline, no HTTP calls."""
import pytest
from llm.ollama_client import _extract_json


def test_extract_json_plain_object():
    raw = '{"action": "wait", "reason": "no setup"}'
    assert _extract_json(raw) == {"action": "wait", "reason": "no setup"}


def test_extract_json_code_fence():
    raw = "Here is my analysis:\n```json\n{\"action\": \"enter\", \"asset\": \"SOL-PERP\"}\n```"
    assert _extract_json(raw) == {"action": "enter", "asset": "SOL-PERP"}


def test_extract_json_code_fence_no_lang():
    raw = "Result:\n```\n{\"action\": \"close\", \"position_id\": \"pos_123\"}\n```"
    assert _extract_json(raw) == {"action": "close", "position_id": "pos_123"}


def test_extract_json_embedded_brace():
    raw = "Some text before { \"action\": \"wait\", \"reason\": \"test\" } and after"
    assert _extract_json(raw) == {"action": "wait", "reason": "test"}


def test_extract_json_invalid_returns_none():
    assert _extract_json("not json at all") is None
    assert _extract_json("") is None
    assert _extract_json("```json\nnot valid\n```") is None


def test_extract_json_nested():
    raw = '{"action": "enter", "key_conditions_met": ["trend up", "volume high"]}'
    result = _extract_json(raw)
    assert result["action"] == "enter"
    assert result["key_conditions_met"] == ["trend up", "volume high"]


def test_extract_json_with_prefix():
    raw = 'Here is my analysis.\n{"action": "wait", "reason": "no signal"}'
    assert _extract_json(raw) == {"action": "wait", "reason": "no signal"}
