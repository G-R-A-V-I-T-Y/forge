"""Tests for llm/model_chain.py's ordered fallback logic.

All opencode subprocess calls and the Ollama tier are mocked — no
network/real processes, deterministic and fast, CI-safe. See
scripts/verify_model_chain_live.py for the real (unmocked) verification
run against the actual opencode binary and Ollama service, whose results
are recorded in the PR description and design doc.
"""
import subprocess
from unittest.mock import patch

from llm import model_chain

SYSTEM = "You are a trader."
PROMPT = "What should we do with SOL?"


def _ndjson_text_event(text: str) -> str:
    return '{"type":"text","part":{"type":"text","text":' + _json_escape(text) + "}}"


def _json_escape(s: str) -> str:
    import json
    return json.dumps(s)


def _fake_completed_process(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["opencode"], returncode=returncode, stdout=stdout, stderr=stderr)


def _valid_decision_ndjson(action="wait", reason="ok") -> str:
    import json
    payload = json.dumps({"action": action, "reason": reason})
    return _ndjson_text_event(payload) + "\n"


def test_tier1_succeeds_later_tiers_never_attempted():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _fake_completed_process(_valid_decision_ndjson())
        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert decision == {"action": "wait", "reason": "ok"}
    assert model_used == "Claude Sonnet 5 (low)"
    assert mock_run.call_count == 1
    called_cmd = mock_run.call_args.args[0]
    assert "openrouter/anthropic/claude-sonnet-5" in called_cmd
    assert "--variant" in called_cmd and "low" in called_cmd


def test_tier1_times_out_tier2_succeeds():
    def side_effect(cmd, **kwargs):
        if "openrouter/anthropic/claude-sonnet-5" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
        return _fake_completed_process(_valid_decision_ndjson())

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert decision == {"action": "wait", "reason": "ok"}
    assert model_used == "DeepSeek V4 Flash Free"
    assert mock_run.call_count == 2


def test_tier1_fails_nonzero_exit_tier2_succeeds():
    def side_effect(cmd, **kwargs):
        if "openrouter/anthropic/claude-sonnet-5" in cmd:
            return _fake_completed_process("", returncode=1, stderr="boom")
        return _fake_completed_process(_valid_decision_ndjson())

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert model_used == "DeepSeek V4 Flash Free"
    assert mock_run.call_count == 2


def test_all_opencode_tiers_fail_falls_through_to_ollama():
    with patch("subprocess.run") as mock_run, \
         patch("llm.client._ollama_decide") as mock_ollama:
        mock_run.return_value = _fake_completed_process("", returncode=1, stderr="down")
        mock_ollama.return_value = {"action": "wait", "reason": "ollama said wait"}

        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert mock_run.call_count == 6  # all 6 opencode tiers attempted
    mock_ollama.assert_called_once()
    assert decision == {"action": "wait", "reason": "ollama said wait"}
    assert model_used == "Ollama qwen3.6:35b_optimized"


def test_all_tiers_fail_including_ollama_returns_explicit_error():
    with patch("subprocess.run") as mock_run, \
         patch("llm.client._ollama_decide") as mock_ollama:
        mock_run.return_value = _fake_completed_process("", returncode=1, stderr="down")
        # This is llm/client.py's _ollama_decide() failure sentinel — the
        # literal string model_chain.py's _run_ollama_tier() detects.
        mock_ollama.return_value = {"action": "wait", "reason": "LLM unavailable or timed out"}

        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert mock_run.call_count == 6
    mock_ollama.assert_called_once()
    assert decision == {"action": "error", "reason": "no model available"}
    assert model_used is None


def test_tier_with_unparseable_json_falls_through():
    def side_effect(cmd, **kwargs):
        if "openrouter/anthropic/claude-sonnet-5" in cmd:
            return _fake_completed_process(_ndjson_text_event("not valid json at all") + "\n")
        return _fake_completed_process(_valid_decision_ndjson())

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert model_used == "DeepSeek V4 Flash Free"


def test_tier_with_missing_required_enter_fields_falls_through():
    import json
    incomplete = _ndjson_text_event(json.dumps({"action": "enter", "asset": "SOL-PERP"})) + "\n"

    def side_effect(cmd, **kwargs):
        if "openrouter/anthropic/claude-sonnet-5" in cmd:
            return _fake_completed_process(incomplete)
        return _fake_completed_process(_valid_decision_ndjson())

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert model_used == "DeepSeek V4 Flash Free"


def test_tier_error_event_falls_through():
    def side_effect(cmd, **kwargs):
        if "openrouter/anthropic/claude-sonnet-5" in cmd:
            return _fake_completed_process('{"type":"error","error":{"name":"UnknownError"}}\n')
        return _fake_completed_process(_valid_decision_ndjson())

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        decision, model_used = model_chain.decide(SYSTEM, PROMPT, config={})

    assert model_used == "DeepSeek V4 Flash Free"


def test_chain_order_and_display_names():
    kinds = [t.kind for t in model_chain.CHAIN]
    assert kinds == ["opencode"] * 6 + ["ollama"]
    display_names = [t.display_name for t in model_chain.CHAIN]
    assert display_names == [
        "Claude Sonnet 5 (low)",
        "DeepSeek V4 Flash Free",
        "Big Pickle",
        "MiMo V2.5 Free",
        "North Mini Code Free",
        "Nemotron 3 Ultra Free",
        "Ollama qwen3.6:35b_optimized",
    ]
