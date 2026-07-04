"""Tests for llm/llama_server_client.py."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from llm import llama_server_client


def _make_response(content: str, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    resp.raise_for_status = MagicMock()
    return resp


class MockAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, json=None, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_returns_parsed_json():
    payload = json.dumps({"action": "wait", "reason": "test"})
    resp = _make_response(payload)

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = await llama_server_client.decide("sys", "user")

    assert result == {"action": "wait", "reason": "test"}


@pytest.mark.asyncio
async def test_returns_none_on_timeout():
    import httpx

    class TimeoutClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *a, **kw): raise httpx.TimeoutException("timeout")

    with patch("httpx.AsyncClient", return_value=TimeoutClient()):
        result = await llama_server_client.decide("sys", "user")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_empty_content():
    resp = _make_response("")

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = await llama_server_client.decide("sys", "user")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_malformed_json():
    resp = _make_response("this is not json at all")

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = await llama_server_client.decide("sys", "user")

    assert result is None


@pytest.mark.asyncio
async def test_uses_provided_port():
    payload = json.dumps({"action": "wait", "reason": "ok"})
    resp = _make_response(payload)
    captured_url = []

    class CapturingClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kw):
            captured_url.append(url)
            return resp

    with patch("httpx.AsyncClient", return_value=CapturingClient()):
        await llama_server_client.decide("sys", "user", port=9999)

    assert "9999" in captured_url[0]


@pytest.mark.asyncio
async def test_extracts_json_from_markdown_block():
    content = '```json\n{"action": "enter", "asset": "SOL-PERP", "direction": "long", "entry_price": 100, "stop_loss_price": 90, "leverage": 3, "position_size_pct": 0.1}\n```'
    resp = _make_response(content)

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = await llama_server_client.decide("sys", "user")

    assert result is not None
    assert result["action"] == "enter"
