"""Tests for llm/ollama_client.py — all offline, no HTTP calls."""
import httpx
import pytest
import respx

from llm.ollama_client import _extract_json, decide, MODEL, OLLAMA_URL, TIMEOUT_SECS


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


@pytest.mark.asyncio
@respx.mock
async def test_decide_uses_configured_model():
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    result = await decide("sys", "prompt", config={"llm_model": "qwen3.6:35b_optimized"})
    assert result == {"action": "wait", "reason": "ok"}
    sent_payload = route.calls.last.request.content
    import json as _json
    assert _json.loads(sent_payload)["model"] == "qwen3.6:35b_optimized"


@pytest.mark.asyncio
@respx.mock
async def test_decide_falls_back_to_default_model_when_config_missing():
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    result = await decide("sys", "prompt", config=None)
    assert result == {"action": "wait", "reason": "ok"}
    sent_payload = route.calls.last.request.content
    import json as _json
    assert _json.loads(sent_payload)["model"] == MODEL == "qwen3.6:35b_optimized"


def test_timeout_secs_gives_headroom_for_concurrent_agent_queueing():
    # forge.py's fleet cycle spawns every agent (target_agent_count, up to
    # 10) concurrently via asyncio.gather; when several agents fall through
    # the opencode chain to this Ollama tier around the same time, their
    # requests queue against the single local model instance. A single
    # unloaded qwen3.6:35b request already measures 121-194s (see MODEL
    # comment above), so a handful of queued requests can exceed a 300s
    # timeout even though the Ollama server itself is up and healthy.
    assert TIMEOUT_SECS >= 900.0


@pytest.mark.asyncio
@respx.mock
async def test_decide_falls_back_to_default_model_when_key_absent():
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    result = await decide("sys", "prompt", config={"llm_backend": "ollama"})
    assert result == {"action": "wait", "reason": "ok"}
    sent_payload = route.calls.last.request.content
    import json as _json
    assert _json.loads(sent_payload)["model"] == MODEL


@pytest.mark.asyncio
@respx.mock
async def test_decide_disables_thinking_by_default():
    """qwen3.6 is a thinking model; without "think": false every decision
    burns 120-290s of reasoning tokens (measured live: 13s with it, and it
    is the reason the control arm can run on this tier at all)."""
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    result = await decide("sys", "prompt", config={})
    assert result == {"action": "wait", "reason": "ok"}
    import json as _json
    payload = _json.loads(route.calls.last.request.content)
    assert payload["think"] is False


@pytest.mark.asyncio
@respx.mock
async def test_decide_think_configurable():
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    await decide("sys", "prompt", config={"llm_think": True})
    import json as _json
    payload = _json.loads(route.calls.last.request.content)
    assert payload["think"] is True


@pytest.mark.asyncio
@respx.mock
async def test_decide_sends_keep_alive():
    """Default Ollama keep_alive is 5m -- the same cadence as forge.py's
    heartbeat cycle, so a cycle that starts a few seconds late pays a full
    reload of this 36B model before it can even start inferring. An
    explicit longer keep_alive keeps it resident across idle time between
    cycles."""
    route = respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"action": "wait", "reason": "ok"}'}}
        )
    )
    await decide("sys", "prompt")
    import json as _json
    payload = _json.loads(route.calls.last.request.content)
    assert payload["keep_alive"] == "30m"
