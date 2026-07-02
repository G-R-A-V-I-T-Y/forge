"""Tests for forge.py's _spawn_agent_runner per-agent subprocess timeout.

Live testing (see docs/superpowers/specs/2026-07-01-model-fallback-chain-design.md
follow-up investigation) found the opencode tiers can burn up to ~240s on
failing/hanging free models before ever reaching the Ollama tier, and the
Ollama tier itself needs headroom for concurrent-agent queueing (see
tests/test_ollama_client.py). The outer per-agent kill in forge.py must
exceed that worst case, or a real (but slow) Qwen answer gets killed before
it's captured.
"""
import asyncio

import pytest

import forge


@pytest.mark.asyncio
async def test_spawn_agent_runner_timeout_exceeds_ollama_headroom(monkeypatch):
    captured_timeout = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"AGENT_RESULT [a1] action=wait detail=ok\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    async def fake_wait_for(coro, timeout):
        captured_timeout["value"] = timeout
        return await coro

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    result = await forge._spawn_agent_runner("a1", "db.sqlite", "config.yaml")

    assert result == {"agent_id": "a1", "action": "wait", "detail": "ok"}
    # Must comfortably exceed llm.ollama_client.TIMEOUT_SECS (900s) plus
    # room for the opencode tiers that run before it.
    assert captured_timeout["value"] >= 1200
