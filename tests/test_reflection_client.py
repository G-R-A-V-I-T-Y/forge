"""Tests for llm/reflection_client.py -- the dedicated raw-text completion
transport for the reflection pipeline (M9 criteria 1+2).

Unlike llm/llama_server_client.py (patched over the same HTTP layer in
tests/test_llama_server_client.py), this transport must NOT coerce its
response through JSON extraction or decision-schema validation -- the
whole point of its existence is that spec YAML / diagnosis text is not a
trade decision and must not be rejected as one.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

from llm import reflection_client

FORGE_PY = Path(__file__).resolve().parents[1] / "forge.py"


def _make_response(content: str, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status = MagicMock()
    return resp


class MockAsyncClient:
    """Mirrors tests/test_llama_server_client.py's mock pattern."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, json=None, **kwargs):
        return self._response


def test_complete_returns_raw_text(monkeypatch):
    """The transport returns the model's raw text verbatim: spec-YAML
    output is neither coerced into nor rejected as a trade decision.

    llm/llama_server_client.py's decide() would run this exact same
    response through _extract_json/_is_valid_decision and reject it (no
    'action' key) -- reflection_client.complete() must not do that.
    """
    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    spec_yaml = (
        "agent_id: test_agent\n"
        "spec_version: 2\n"
        "entry:\n"
        "  direction: long\n"
        "  evidence: []\n"
    )
    resp = _make_response(spec_yaml)

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = reflection_client.complete("system prompt", "user prompt")

    # Exact byte-for-byte passthrough -- no JSON parsing, no schema check,
    # no dict/tuple coercion of any kind.
    assert result == spec_yaml
    assert isinstance(result, str)


def test_complete_returns_empty_string_on_timeout(monkeypatch):
    """Matches the documented contract: never raises, returns "" on failure."""
    import httpx

    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    class TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *a, **kw):
            raise httpx.TimeoutException("timeout")

    with patch("httpx.AsyncClient", return_value=TimeoutClient()):
        result = reflection_client.complete("system prompt", "user prompt")

    assert result == ""


def test_scheduler_uses_reflection_transport():
    """forge.py's _run_reflection_scheduler_job must pass the reflection
    transport (llm.reflection_client.complete) as llm_fn -- never
    model_chain.decide, which validates every response as a trade decision
    and would silently reject every reflection response (the M9 bug this
    task fixes).

    Reads forge.py's source directly instead of importing the module,
    because forge.py imports apscheduler, which is not installed in this
    Python environment (see CLAUDE.md "Local LLM server" notes on
    test_forge_agent_timeout.py / test_forge_heartbeat_schedule.py).
    """
    source = FORGE_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(FORGE_PY))

    job_node = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_run_reflection_scheduler_job"
        ):
            job_node = node
            break

    assert job_node is not None, "_run_reflection_scheduler_job not found in forge.py"

    segment = ast.get_source_segment(source, job_node)
    assert segment is not None

    assert "reflection_client" in segment, (
        "expected llm.reflection_client to be imported/used inside "
        "_run_reflection_scheduler_job"
    )
    assert "reflection_complete" in segment, (
        "expected the reflection transport to be passed as llm_fn"
    )
    assert "model_chain" not in segment, (
        "model_chain.decide validates every response as a trade decision "
        "and must never be used as the reflection llm_fn"
    )
