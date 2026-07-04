"""llm/model_chain.py — ordered model fallback chain for trading decisions.

Every agent's decision call tries an ordered list of models, using the
first one that's actually available/reachable, and reports which model
responded. Tiers 1-6 are routed through the `opencode` CLI (subprocess);
tier 7 is the forge-managed local llama-server (see llm/llama_server.py
and llm/llama_server_client.py), which supersedes the old Ollama tier.
If every tier fails, `decide()` returns an explicit error result rather
than silently degrading to a generic "wait".

The CHAIN list is dynamically loaded from the settings DB on each
`decide()` call so that Settings → Save & Apply takes effect immediately
in the next agent cycle without a forge restart. The hardcoded CHAIN
below is the fallback when no DB is available.

See docs/superpowers/specs/2026-07-01-model-fallback-chain-design.md for
the full design rationale, including how the `opencode run --format json`
NDJSON output shape was verified directly against the real binary.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Coroutine, TypeVar
from typing import NamedTuple

from llm.ollama_client import _extract_json

_T = TypeVar("_T")


def _run_coroutine_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run an async coroutine to completion from synchronous code.

    decide() is called synchronously from inside agents/decision_loop.py's
    running event loop (agents/agent_runner.py's asyncio.run(_run_once(...))),
    so asyncio.run() here would raise "cannot be called from a running event
    loop". Running the coroutine on a dedicated thread with its own event
    loop avoids that regardless of whether a loop is already running.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result["value"]

logger = logging.getLogger(__name__)


def _resolve_opencode_executable() -> str:
    """Resolve the real opencode executable to invoke directly, bypassing
    Windows' cmd.exe entirely.

    On Windows, `opencode` on PATH is an `opencode.cmd` batch shim (npm
    global bin). Two problems, both found via live verification against
    the real binary:

    1. subprocess.run(["opencode", ...]) without shell=True fails with
       WinError 2 ("cannot find the file") because Windows CreateProcess
       doesn't consult PATHEXT the way cmd.exe does. shutil.which() *does*
       resolve PATHEXT, giving the real `opencode.CMD` path.

    2. Critically, invoking a .cmd/.bat file at all — even via
       shutil.which()'s resolved path — routes the call through cmd.exe,
       whose command-line parser cannot contain a raw newline: any
       argument containing "\n" (i.e. every real multi-paragraph decision
       prompt) gets silently truncated at the first newline. This was
       diagnosed by feeding a deliberately newline-containing test string
       through the .cmd path and observing corruption, then confirming a
       clean round-trip when calling the batch shim's own wrapped .exe
       directly instead. This is the actual reason production-shaped
       prompts got "no market data provided" responses in live
       verification — models were receiving only the prompt's first line.

    Fix: parse the .cmd shim's own script (it's a fixed two-line
    "CALL :find_dp0" + a quoted path to a bundled .exe, per npm's standard
    .cmd shim template) to find the real .exe it wraps, and invoke that
    .exe directly — a real Windows executable's CreateProcess argv
    marshaling has no such newline restriction. Falls back to the
    shutil.which() result (or the literal "opencode") if parsing fails,
    preserving the old (newline-unsafe, but at least invocable) behavior
    rather than crashing.
    """
    resolved = shutil.which("opencode") or "opencode"
    if not resolved.lower().endswith((".cmd", ".bat")):
        return resolved  # non-Windows, or already resolves straight to an exe/script

    try:
        shim_text = Path(resolved).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return resolved

    # npm's standard .cmd shim ends with: "%dp0%\node_modules\...\opencode.exe" %*
    match = re.search(r'"([^"]+\.exe)"', shim_text)
    if not match:
        logger.warning(
            "Could not find a wrapped .exe inside opencode shim %s; "
            "falling back to the .cmd shim (newline-containing prompts "
            "will be truncated by cmd.exe)", resolved,
        )
        return resolved

    exe_path = match.group(1).replace("%dp0%", str(Path(resolved).parent) + "\\")
    if not Path(exe_path).exists():
        logger.warning(
            "Resolved opencode .exe path %s does not exist; falling back "
            "to the .cmd shim", exe_path,
        )
        return resolved

    return exe_path


_OPENCODE_EXECUTABLE = _resolve_opencode_executable()

# Free-tier/rate-limited remote models can be slow or occasionally down.
# 60s per tier is generous enough to absorb normal latency (all 6 real
# remote tiers measured well under 30s in live verification — see the PR
# description) without letting one dead tier stall a decision cycle for
# minutes. On timeout we log a warning and fall through to the next tier.
OPENCODE_TIMEOUT_SECS = 60

# opencode's default "build" agent identifies itself as a coding assistant
# with a strong baked-in system prompt and full filesystem/tool access.
# Live verification (see design doc) found this made every remote tier
# unreliable for trading decisions: weaker free models responded with
# generic {"status": "ready", ...} acknowledgments instead of a decision,
# and Claude Sonnet explicitly refused to "roleplay" as a trading API.
# `trading-responder` is a project-scoped custom agent (committed at
# .opencode/agent/trading-responder.md, auto-discovered by opencode from
# the working directory) with all tool permissions denied and a system
# prompt that replaces opencode's coding-assistant identity with a
# headless JSON-decision-responder identity. Every opencode-routed tier
# uses it via --agent.
OPENCODE_AGENT = "trading-responder"

# The literal failure sentinel llm/client.py's _ollama_decide() returns
# when the Ollama tier itself failed (timeout, connection error,
# unparseable JSON) rather than the model legitimately choosing to wait.
# model_chain.decide() must distinguish "Ollama really said wait" from
# "Ollama was unreachable" to know whether to fall through to the final
# "no model available" result — see the design doc for why this couples
# to the literal reason string instead of reimplementing Ollama calling.
_OLLAMA_FAILURE_REASON = "LLM unavailable or timed out"

_REQUIRED_ENTER_FIELDS = (
    "asset", "direction", "entry_price", "stop_loss_price", "leverage", "position_size_pct",
)


class Tier(NamedTuple):
    kind: str  # "opencode", "ollama", or "llama_server"
    model_id: str | None  # None for local tiers
    variant: str | None  # e.g. "low"; None if not applicable
    display_name: str


# Hardcoded fallback — used when the settings DB is unavailable.
# The dynamic chain loaded from settings supersedes this at runtime.
CHAIN: list[Tier] = [
    Tier("opencode", "openrouter/anthropic/claude-sonnet-5", "low", "Claude Sonnet 5 (low)"),
    Tier("opencode", "opencode/deepseek-v4-flash-free", None, "DeepSeek V4 Flash Free"),
    Tier("opencode", "opencode/big-pickle", None, "Big Pickle"),
    Tier("opencode", "opencode/mimo-v2.5-free", None, "MiMo V2.5 Free"),
    Tier("opencode", "opencode/north-mini-code-free", None, "North Mini Code Free"),
    Tier("opencode", "opencode/nemotron-3-ultra-free", None, "Nemotron 3 Ultra Free"),
    Tier("llama_server", None, None, "Local llama-server (Qwen3.6)"),
]

_SETTINGS_DB_PATH = "data/forge.db"


def _load_chain_from_settings() -> list[Tier] | None:
    """Try to read the model_chain list from the settings DB.

    Returns a list of Tier objects if the DB is readable and has a
    model_chain setting, otherwise None so the caller falls back to CHAIN.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(_SETTINGS_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'model_chain'"
        ).fetchone()
        conn.close()
        if row is None:
            return None
        chain_dicts = json.loads(row["value"])
        return [
            Tier(
                kind=d.get("kind", "opencode"),
                model_id=d.get("model_id"),
                variant=d.get("variant"),
                display_name=d.get("display_name", d.get("model_id", "unknown")),
            )
            for d in chain_dicts
        ]
    except Exception:
        return None


def get_chain() -> list[Tier]:
    """Return the active model chain, preferring the settings DB over CHAIN."""
    loaded = _load_chain_from_settings()
    return loaded if loaded is not None else CHAIN


def _is_valid_decision(decision: dict) -> bool:
    """Same shape check agents/decision_loop.py's _call_llm_with_retry()
    applies: a recognized action, and (for "enter") all required trade
    parameters present."""
    if not isinstance(decision, dict):
        return False
    action = decision.get("action")
    if action not in ("enter", "wait", "close"):
        return False
    if action == "enter":
        return all(k in decision for k in _REQUIRED_ENTER_FIELDS)
    return True


def _run_opencode_tier(model_id: str, variant: str | None, message: str) -> dict | None:
    """Run one opencode-routed tier as a subprocess. Never raises — returns
    None on timeout, non-zero exit, unparseable output, or a decision
    missing required fields, logging a warning in each case."""
    cmd = [
        _OPENCODE_EXECUTABLE, "run", "--model", model_id,
        "--agent", OPENCODE_AGENT, "--format", "json",
    ]
    if variant:
        cmd += ["--variant", variant]
    cmd.append(message)

    try:
        # encoding="utf-8" (with errors="replace") is required on Windows:
        # subprocess.run's default text-mode decoding uses the console's
        # locale codepage (cp1252 in this environment), which raises
        # UnicodeDecodeError on non-ASCII bytes opencode writes to stdout
        # (observed live: it silently killed the stdout-reader thread,
        # stalling the call until the 60s timeout instead of returning
        # promptly). errors="replace" keeps a single bad byte from losing
        # an otherwise-valid NDJSON response.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=OPENCODE_TIMEOUT_SECS,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        logger.warning("opencode tier %s timed out after %ds", model_id, OPENCODE_TIMEOUT_SECS)
        return None
    except Exception as exc:
        logger.warning("opencode tier %s failed to launch: %s", model_id, exc)
        return None

    if proc.returncode != 0:
        logger.warning("opencode tier %s exited %d: %.300s", model_id, proc.returncode, proc.stderr)
        return None

    text_parts: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "error":
            logger.warning("opencode tier %s returned an error event: %.300s", model_id, event)
            return None
        if event.get("type") == "text":
            part = event.get("part") or {}
            text = part.get("text")
            if text:
                text_parts.append(text)

    if not text_parts:
        logger.warning("opencode tier %s produced no text output", model_id)
        return None

    full_text = "".join(text_parts)
    decision = _extract_json(full_text)
    if decision is None:
        logger.warning("opencode tier %s: could not extract JSON from response", model_id)
        return None

    if not _is_valid_decision(decision):
        logger.warning("opencode tier %s returned an invalid decision shape: %r", model_id, decision)
        return None

    return decision


def _run_ollama_tier(system_prompt: str, decision_prompt: str, config: dict | None) -> dict | None:
    """Call the existing Ollama mechanism unchanged via llm/client.py's
    _ollama_decide(). Detects the failure sentinel (see
    _OLLAMA_FAILURE_REASON above) to distinguish a real failure from a
    legitimate "wait" decision."""
    from llm.client import _ollama_decide

    result = _ollama_decide(system_prompt, decision_prompt, config)
    if result.get("action") == "wait" and result.get("reason") == _OLLAMA_FAILURE_REASON:
        return None
    if not _is_valid_decision(result):
        return None
    return result


def _run_llama_server_tier(
    system_prompt: str, decision_prompt: str, config: dict | None
) -> dict | None:
    """Call the forge-managed llama-server via its OpenAI-compatible endpoint.

    The port is read from the settings DB at call time so a Settings →
    Save & Apply takes effect immediately in the next agent cycle.
    """
    from llm import llama_server_client

    port = _DEFAULT_LLAMA_PORT
    try:
        import sqlite3
        conn = sqlite3.connect(_SETTINGS_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'llama_server_port'"
        ).fetchone()
        conn.close()
        if row is not None:
            port = int(json.loads(row["value"]))
    except Exception:
        pass

    try:
        result = _run_coroutine_sync(
            llama_server_client.decide(system_prompt, decision_prompt, port=port)
        )
    except Exception as exc:
        logger.error("llama-server tier failed: %s", exc)
        return None

    if result is None:
        return None
    if not _is_valid_decision(result):
        logger.warning(
            "llama-server tier returned an invalid decision shape: %r", result
        )
        return None
    return result


# Default port when settings DB is unavailable.
_DEFAULT_LLAMA_PORT = 8080


def decide(
    system_prompt: str,
    decision_prompt: str,
    config: dict | None = None,
) -> tuple[dict, str | None]:
    """Try the ordered model chain, returning (decision_dict, model_display_name)
    for the first tier that succeeds. If every tier fails, returns
    ({"action": "error", "reason": "no model available"}, None) — never a
    silent generic "wait", per the captain's explicit requirement.

    The chain is loaded from the settings DB on each call so that
    Settings → Save & Apply takes effect without a forge restart.

    Synchronous, matching llm/client.py's _ollama_decide() sync-wrapping
    pattern and agents/decision_loop.py's non-awaited llm_fn(...) calling
    convention.
    """
    message = f"{system_prompt}\n\n{decision_prompt}"
    chain = get_chain()

    for tier in chain:
        if tier.kind == "opencode":
            decision = _run_opencode_tier(tier.model_id, tier.variant, message)
        elif tier.kind == "llama_server":
            decision = _run_llama_server_tier(system_prompt, decision_prompt, config)
        else:
            decision = _run_ollama_tier(system_prompt, decision_prompt, config)

        if decision is not None:
            logger.info("Model chain: %s answered", tier.display_name)
            return decision, tier.display_name

    logger.error("Model chain: all tiers failed, no model available")
    return {"action": "error", "reason": "no model available"}, None
