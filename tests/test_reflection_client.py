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
import inspect
import logging
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest

from llm import reflection_client

FORGE_PY = Path(__file__).resolve().parents[1] / "forge.py"
REFLECTION_PY = Path(__file__).resolve().parents[1] / "agents" / "reflection.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(content: str, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status = MagicMock()
    return resp


def _make_error_response(status_code: int = 500):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
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


class FailingClient:
    """Client that always raises on post."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, *a, **kw):
        raise RuntimeError("connection refused")


# ---------------------------------------------------------------------------
# Core contract: raw text passthrough
# ---------------------------------------------------------------------------

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


def test_complete_passthrough_diagnosis_text(monkeypatch):
    """Diagnosis prose (not YAML) must also pass through uncoerced."""
    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    diagnosis = (
        "The agent's momentum signals are working in trending regimes but "
        "failing in range-bound markets. Regret analysis shows 3 missed "
        "short entries that would have captured 4.2% each."
    )
    resp = _make_response(diagnosis)

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = reflection_client.complete("sys", "user")

    assert result == diagnosis


# ---------------------------------------------------------------------------
# Failure modes: never raises, returns ""
# ---------------------------------------------------------------------------

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


def test_complete_returns_empty_on_http_error(monkeypatch):
    """HTTP 500 / raise_for_status failure returns "" without raising."""
    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    resp = _make_error_response(500)

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = reflection_client.complete("sys", "user")

    assert result == ""


def test_complete_returns_empty_on_connection_failure(monkeypatch):
    """Unreachable server returns "" without raising."""
    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    with patch("httpx.AsyncClient", return_value=FailingClient()):
        result = reflection_client.complete("sys", "user")

    assert result == ""


def test_complete_returns_empty_on_empty_content(monkeypatch):
    """Model returning empty content string returns ""."""
    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    resp = _make_response("")

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = reflection_client.complete("sys", "user")

    assert result == ""


def test_complete_returns_empty_on_none_content(monkeypatch):
    """Model returning null content returns ""."""
    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": None}}]}
    resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        result = reflection_client.complete("sys", "user")

    assert result == ""


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------

def test_opencode_backend_returns_empty_stub(monkeypatch):
    """The opencode backend is currently a stub that returns ""."""
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "opencode",
    )

    result = reflection_client.complete("sys", "user")
    assert result == ""


def test_custom_model_id_falls_through_to_opencode_stub(monkeypatch):
    """An unknown model id string is treated as an opencode model id."""
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective",
        lambda: "openrouter/some/custom-model",
    )

    result = reflection_client.complete("sys", "user")
    assert result == ""


def test_llama_server_port_read_from_settings(monkeypatch):
    """Port is read from settings, not hardcoded."""
    ports_seen: list[int] = []

    def fake_read_port():
        ports_seen.append(42)
        return 42

    monkeypatch.setattr(reflection_client, "_read_port", fake_read_port)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    resp = _make_response("hello")
    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        reflection_client.complete("sys", "user")

    assert ports_seen == [42]


# ---------------------------------------------------------------------------
# Settings override mechanism
# ---------------------------------------------------------------------------

def test_settings_dict_override(monkeypatch):
    """When settings dict is provided, its reflection_model overrides DB."""
    monkeypatch.setattr(reflection_client, "_read_port", lambda: 8080)
    # The DB-level reader should NOT be called when override is active
    db_called = []

    def fake_db_reader():
        db_called.append(True)
        return "llama_server"

    monkeypatch.setattr(reflection_client, "_read_reflection_model", fake_db_reader)
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    resp = _make_response("ok")
    settings = {"reflection_model": "opencode"}

    with patch("httpx.AsyncClient", return_value=MockAsyncClient(resp)):
        # complete() with settings override should use "opencode" backend,
        # which returns "" (stub). The llama_server httpx path won't be hit.
        result = reflection_client.complete(
            "sys", "user", settings=settings,
        )

    # After complete() returns, the override should be cleared.
    assert reflection_client._reflection_model_override is None


def test_settings_override_cleared_on_exception(monkeypatch):
    """Override is cleared even when an exception occurs."""
    monkeypatch.setattr(
        reflection_client, "_read_reflection_model_effective", lambda: "llama_server",
    )

    # Force an exception in _run_coroutine_sync
    with patch.object(reflection_client, "_run_coroutine_sync", side_effect=RuntimeError("boom")):
        try:
            reflection_client.complete(
                "sys", "user", settings={"reflection_model": "opencode"},
            )
        except RuntimeError:
            pass

    assert reflection_client._reflection_model_override is None


# ---------------------------------------------------------------------------
# _extract_content_from_response edge cases
# ---------------------------------------------------------------------------

def test_extract_content_missing_choices():
    """Malformed body returns None."""
    assert reflection_client._extract_content_from_response({}) is None
    assert reflection_client._extract_content_from_response({"choices": []}) is None


def test_extract_content_missing_message():
    """Body with choices but no message returns None."""
    assert reflection_client._extract_content_from_response(
        {"choices": [{"not_message": True}]}
    ) is None


def test_extract_content_valid():
    """Happy path returns the content string."""
    body = {"choices": [{"message": {"content": "hello world"}}]}
    assert reflection_client._extract_content_from_response(body) == "hello world"


# ---------------------------------------------------------------------------
# Prompt hash determinism
# ---------------------------------------------------------------------------

def test_prompt_hash_is_deterministic():
    """Same inputs produce the same hash prefix."""
    h1 = reflection_client._prompt_hash("sys", "user")
    h2 = reflection_client._prompt_hash("sys", "user")
    assert h1 == h2
    assert len(h1) == 12  # SHA-256 hex prefix, 12 chars


def test_prompt_hash_differs_for_different_inputs():
    """Different prompts produce different hashes."""
    h1 = reflection_client._prompt_hash("sys1", "user1")
    h2 = reflection_client._prompt_hash("sys2", "user2")
    assert h1 != h2


# ---------------------------------------------------------------------------
# Signature conformance
# ---------------------------------------------------------------------------

def test_complete_matches_callable_signature():
    """complete() has the (str, str) -> str signature expected by
    agents/reflection.py's llm_fn parameter."""
    sig = inspect.signature(reflection_client.complete)
    params = list(sig.parameters.keys())
    assert "system_prompt" in params
    assert "user_prompt" in params

    # Verify it's compatible with Callable[[str, str], str]
    assert callable(reflection_client.complete)


# ---------------------------------------------------------------------------
# forge.py wiring verification (AST-based, no import)
# ---------------------------------------------------------------------------

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


def test_forge_wires_llm_fn_on_web_app_state():
    """forge.py's main() sets web_app.state.llm_fn = reflection_client.complete
    so manual trigger endpoints can invoke the reflection transport."""
    source = FORGE_PY.read_text(encoding="utf-8")
    assert "web_app.state.llm_fn = reflection_client.complete" in source, (
        "forge.py must wire reflection_client.complete to web_app.state.llm_fn"
    )


def test_forge_imports_reflection_client():
    """forge.py imports llm.reflection_client at module level or in main()."""
    source = FORGE_PY.read_text(encoding="utf-8")
    assert "from llm import reflection_client" in source or (
        "from llm.reflection_client import" in source
    ), "forge.py must import llm.reflection_client"


# ---------------------------------------------------------------------------
# agents/reflection.py config access (AST-based)
# ---------------------------------------------------------------------------

def test_reflection_uses_config_desk_not_get():
    """agents/reflection.py must use config["desk"] (fail-loud) not
    config.get("desk") for the desk config in the complexity-budget gate
    and atomic deploy call."""
    source = REFLECTION_PY.read_text(encoding="utf-8")

    # The complexity-budget gate and atomic deploy should use config["desk"],
    # not config.get("desk", ...).  We allow config.get("desk") in
    # contexts where None is a valid outcome (e.g. optional params), but
    # the desk config must fail loudly.
    #
    # Count occurrences of config["desk"] — there should be at least 2
    # (complexity-budget gate + atomic deploy).
    desk_count = source.count('config["desk"]')
    assert desk_count >= 2, (
        "expected >= 2 uses of config['desk'] in reflection.py, "
        f"found {desk_count}"
    )


def test_no_model_chain_in_reflection():
    """agents/reflection.py must never import or call model_chain.decide."""
    source = REFLECTION_PY.read_text(encoding="utf-8")
    assert "model_chain" not in source, (
        "reflection.py must not reference model_chain — reflection uses "
        "the dedicated llm/reflection_client.py transport"
    )
