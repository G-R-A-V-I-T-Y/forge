"""Tests for meta/reflection_scheduler.py's eligibility + trigger path and
its integration with agents/reflection.py::run_reflection (M9 criteria 1+2).

Self-contained: builds its own agent/trade fixtures rather than importing
tests/test_reflection.py's helpers, mirroring the pattern in
tests/test_m9_modules.py.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import yaml

import agents.reflection as reflection_module
from backtest.engine import BacktestResult
from backtest.walk_forward import WalkForwardReport
from store.db import insert_agent, insert_trade
from store.specs import deploy_spec as _deploy_spec, get_active_spec, get_challenger_spec


def _passing_wf_report(deflated_sharpe: float = 1.0) -> WalkForwardReport:
    test_result = BacktestResult(sharpe=deflated_sharpe)
    return WalkForwardReport(
        train=BacktestResult(), validate=BacktestResult(), test=test_result,
        deflated_sharpe=deflated_sharpe, parameter_sensitivity={},
    )

AGENT_ID = "reflection_sched_agent"

#: Stage 1 (Diagnose) is plain text, unparsed -- any non-empty string works.
_DIAGNOSE_RESPONSE = "Diagnosis: funding_zscore signal underperforming in ranging regime."


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_reflection.py's fixtures)
# ---------------------------------------------------------------------------


def _trade(agent_id=AGENT_ID, days_ago=0, result="win", pnl_pct=0.05):
    ts = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat().replace("+00:00", "Z")
    return {
        "id": str(uuid4()),
        "agent_id": agent_id,
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 1,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": ts,
        "exit_price": 100.0 * (1 + pnl_pct),
        "exit_timestamp": ts,
        "exit_reason": "tp_hit" if result == "win" else "sl_hit",
        "duration_minutes": 60.0,
        "pnl_pct": pnl_pct,
        "pnl_usd": 5000.0 * pnl_pct,
        "result": result,
        "status": "closed",
        "market_context_json": "{}",
        "agent_reasoning_json": "{}",
        "postmortem": None,
        "hypothesis": "test",
        "key_conditions_met": None,
        "key_conditions_missing": None,
        "confidence": 0.7,
        "expected_value": None,
        "agent_postmortem": None,
        "regime": "normal",
    }


def _insert_trades(conn, agent_id, count):
    """Insert *count* closed trades spanning *count* days (newest last) --
    enough date spread to satisfy the pattern_persistence gate."""
    for i in range(count):
        insert_trade(conn, _trade(agent_id=agent_id, days_ago=count - i))


def _setup_agent(conn, agent_id=AGENT_ID):
    existing = conn.execute(
        "SELECT id FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if not existing:
        insert_agent(conn, agent_id, agent_id, "2026-06-01T00:00:00Z", "{}")


def _valid_spec_yaml(agent_id=AGENT_ID, version=2):
    return yaml.dump(
        {
            "agent_id": agent_id,
            "spec_version": version,
            "thesis_version": 1,
            "universe": {"include": ["SOL-PERP"]},
            "regime_filter": {"exclude": []},
            "entry": {
                "direction": "long",
                "confidence_threshold": 0.7,
                "scale_threshold": 0.5,
                "evidence": [
                    {
                        "name": "funding_signal",
                        "feature": "funding_zscore",
                        "thresholds": [
                            {"op": ">", "value": -1.0, "weight": 0.7},
                            {"op": "else", "weight": 0.0},
                        ],
                        "missing": "veto",
                    }
                ],
                "secondary_evidence": [],
            },
            "exit": {
                "stop_loss_pct": 0.03,
                "take_profit_pct": 0.06,
                "max_hold_hours": 24,
            },
            "position": {"leverage": 2, "position_size_pct": 0.10},
        },
        default_flow_style=False,
    )


def _deploy_initial_spec(conn, agent_id=AGENT_ID):
    """Deploy a v1 spec with deployed_at 30 days in the past, so the
    update-throttle gate sees an old-enough deployment."""
    from backtest.dsl import EvidenceTerm, Spec, Threshold

    spec = Spec(
        agent_id=agent_id,
        spec_version=1,
        thesis_version=1,
        universe_include=["SOL-PERP"],
        regime_exclude=[],
        direction="long",
        confidence_threshold=0.7,
        scale_threshold=0.5,
        evidence=[
            EvidenceTerm(
                name="funding_signal",
                feature="funding_zscore",
                thresholds=[
                    Threshold(op=">", value=-1.0, weight=0.7),
                    Threshold(op="else", weight=0.0),
                ],
                missing="veto",
            )
        ],
        secondary_evidence=[],
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        max_hold_hours=24,
        leverage=2,
        position_size_pct=0.10,
    )
    spec_id = _deploy_spec(
        conn, agent_id, spec, {"max_leverage": 10, "max_position_size_pct": 0.5},
    )
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat().replace("+00:00", "Z")
    conn.execute("UPDATE specs SET deployed_at = ? WHERE id = ?", (old_ts, spec_id))
    conn.commit()


def _make_llm(*responses):
    it = iter(responses)

    def _fn(system_prompt: str, user_prompt: str) -> str:
        return next(it)

    return _fn


def _redirect_specs_dir(monkeypatch, tmp_path):
    import store.specs as specs_module
    import agents.reflection as reflection_module

    monkeypatch.setattr(specs_module, "SPECS_DIR", tmp_path / "agents" / "specs")
    monkeypatch.setattr(reflection_module, "_THESES_DIR", tmp_path / "agents" / "theses")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_trigger_fires_on_trade_count(conn):
    """With trigger 'every 20 trades', an agent crossing 20 post-deploy
    trades becomes eligible for a reflection cycle; one that hasn't does
    not (drives meta/reflection_scheduler.py's eligibility+trigger path)."""
    from meta.reflection_scheduler import check_agent_eligible, get_reflection_trigger

    _setup_agent(conn)
    trigger = get_reflection_trigger(conn)
    assert trigger["mode"] == "trade_count"
    assert trigger["trade_interval"] == 20

    _insert_trades(conn, AGENT_ID, 19)
    eligible, reason = check_agent_eligible(conn, AGENT_ID, trigger)
    assert eligible is False, reason

    _insert_trades(conn, AGENT_ID, 1)  # crosses the 20-trade threshold
    eligible, reason = check_agent_eligible(conn, AGENT_ID, trigger)
    assert eligible is True, reason


def test_benchmark_agents_never_reflect(conn):
    """Benchmark agents (id starts with 'benchmark_') are permanent
    baselines -- their trade history IS the null distribution for every
    significance test. They must never reflect, even when trade count alone
    would otherwise cross the trigger threshold.

    Covers both protection layers: (a) check_agent_eligible, which gates the
    scheduler path, and (b) run_reflection's own top guard, which protects
    the manual single-agent web trigger (a path that never calls
    check_agent_eligible at all)."""
    from meta.reflection_scheduler import check_agent_eligible, get_reflection_trigger
    from agents.reflection import run_reflection

    benchmark_id = "benchmark_random_walk"
    _setup_agent(conn, agent_id=benchmark_id)
    _insert_trades(conn, benchmark_id, 25)  # crosses the default 20-trade trigger

    trigger = get_reflection_trigger(conn)
    eligible, reason = check_agent_eligible(conn, benchmark_id, trigger)
    assert eligible is False
    assert "benchmark" in reason.lower()

    # run_reflection's own guard must short-circuit before touching the LLM
    # or deploying anything -- track calls to prove it never invokes llm_fn.
    calls = []

    def _llm(system_prompt, user_prompt):
        calls.append((system_prompt, user_prompt))
        return _DIAGNOSE_RESPONSE

    config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
    result = run_reflection(conn, benchmark_id, config, _llm)

    assert result.triggered is False
    assert result.deployed is False
    assert result.blocked_by_gate is not None
    assert "benchmark" in result.blocked_by_gate.lower()
    assert calls == [], "run_reflection must not call the LLM for benchmark agents"
    assert get_active_spec(conn, benchmark_id) is None


def test_rejected_revision_logged_with_gate(conn, monkeypatch, tmp_path):
    """A gated rejection appears in the reflections log, naming the
    blocking gate."""
    from meta.reflection_scheduler import run_reflection_cycle

    _redirect_specs_dir(monkeypatch, tmp_path)
    _setup_agent(conn)
    _deploy_initial_spec(conn)
    # 35 trades all entered "today" -- min_trades/holdout pass, but
    # pattern_persistence fails (only 1 window of 7 days).
    for _ in range(35):
        insert_trade(conn, _trade(days_ago=0))

    llm = _make_llm(
        _DIAGNOSE_RESPONSE, _valid_spec_yaml(version=2), "No critical flaws found.",
    )
    config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
    result = run_reflection_cycle(conn, AGENT_ID, config, llm)

    assert result["triggered"] is True
    assert result["deployed"] is False
    assert result["blocked_by_gate"] == "pattern_persistence"

    row = conn.execute(
        "SELECT outcome, rejection_reason FROM reflections WHERE agent_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (AGENT_ID,),
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "rejected"
    assert row["rejection_reason"] is not None
    assert "pattern_persistence" in row["rejection_reason"]


def test_accepted_revision_hot_deploys(conn, monkeypatch, tmp_path):
    """An accepted revision deploys as a CHALLENGER (M10 crit 5 contract) --
    live for shadow evaluation on the agent's next decision without
    restart, via store/specs.py's get_challenger_spec, while the incumbent
    stays active. (Challenger *resolution* -- regret comparison and
    promotion -- is a later task.)"""
    from meta.reflection_scheduler import run_reflection_cycle

    _redirect_specs_dir(monkeypatch, tmp_path)
    _setup_agent(conn)
    _deploy_initial_spec(conn)
    _insert_trades(conn, AGENT_ID, 35)

    monkeypatch.setattr(
        reflection_module, "run_walk_forward",
        lambda *a, **k: _passing_wf_report(),
    )

    llm = _make_llm(
        _DIAGNOSE_RESPONSE,
        _valid_spec_yaml(version=2),
        "No critical flaws found.",
    )
    config = {
        "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
        "ledger_dir": str(tmp_path / "ledger"),
    }
    result = run_reflection_cycle(conn, AGENT_ID, config, llm)

    assert result["triggered"] is True
    assert result["deployed"] is True, result.get("rejection_reason") or result.get("blocked_by_gate")
    assert result["spec_version"] == 2

    # Incumbent v1 is untouched and still active.
    active = get_active_spec(conn, AGENT_ID)
    assert active is not None
    assert active.spec_version == 1

    # v2 is live as challenger, available for shadow evaluation immediately.
    challenger = get_challenger_spec(conn, AGENT_ID)
    assert challenger is not None
    assert challenger.spec_version == 2
