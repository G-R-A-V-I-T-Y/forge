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

from store.db import insert_agent, insert_trade
from store.specs import deploy_spec as _deploy_spec, get_active_spec

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

    monkeypatch.setattr(specs_module, "SPECS_DIR", tmp_path / "agents" / "specs")


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
    """An accepted revision's spec is active on the agent's next decision
    without restart -- store/specs.py's active-spec lookup returns the new
    version immediately after run_reflection (via run_reflection_cycle)
    accepts it.

    NOTE: the in-tree pipeline deploys accepted revisions directly to
    'active' status (no challenger staging yet) -- this asserts current
    behavior. A later task moves acceptance to challenger status and will
    update this test.
    """
    from meta.reflection_scheduler import run_reflection_cycle

    _redirect_specs_dir(monkeypatch, tmp_path)
    _setup_agent(conn)
    _deploy_initial_spec(conn)
    _insert_trades(conn, AGENT_ID, 35)

    llm = _make_llm(
        _DIAGNOSE_RESPONSE,
        _valid_spec_yaml(version=2),
        "No critical flaws found.",
    )
    config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
    result = run_reflection_cycle(conn, AGENT_ID, config, llm)

    assert result["triggered"] is True
    assert result["deployed"] is True, result.get("rejection_reason") or result.get("blocked_by_gate")
    assert result["spec_version"] == 2

    active = get_active_spec(conn, AGENT_ID)
    assert active is not None
    assert active.spec_version == 2
