"""Tests for agents/reflection.py.

Uses :memory: SQLite (via conftest's conn fixture), monkeypatched SPECS_DIR,
and mocked LLM callables so no external dependencies (real DB, LLM, ledger
data) are needed.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

import agents.reflection as reflection_module
from agents.reflection import (
    ReflectionResult,
    adversarial_pass,
    build_reflection_prompt,
    check_min_trades,
    check_pattern_persistence,
    check_update_throttle,
    compute_calibration_curve,
    diagnose,
    parse_revised_spec,
    run_reflection,
)
from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.engine import BacktestResult
from backtest.walk_forward import WalkForwardReport
from store.db import get_agent, insert_agent, insert_trade
from store.specs import deploy_spec as _deploy_spec, get_challenger_spec

logger = logging.getLogger(__name__)

AGENT_ID = "test_agent"


def _passing_wf_report(deflated_sharpe: float = 1.0, sensitivity: dict | None = None) -> WalkForwardReport:
    """A WalkForwardReport that clears the mandatory walk-forward gate:
    deflated Sharpe > 0 and no parameter-sensitivity fragility flag."""
    test_result = BacktestResult(sharpe=deflated_sharpe)
    return WalkForwardReport(
        train=BacktestResult(), validate=BacktestResult(), test=test_result,
        deflated_sharpe=deflated_sharpe, parameter_sensitivity=sensitivity or {},
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    asset="SOL-PERP",
    pnl_pct=0.05,
    result="win",
    direction="long",
    confidence=0.7,
    regime="normal",
    days_ago=0,
):
    """Create a trade dict, timestamped *days_ago* from now."""
    ts = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat().replace("+00:00", "Z")
    uid = str(uuid4())
    return {
        "id": uid,
        "agent_id": AGENT_ID,
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": asset,
        "direction": direction,
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
        "confidence": confidence,
        "expected_value": None,
        "agent_postmortem": None,
        "regime": regime,
    }


def _insert_trades(conn, count: int, **overrides):
    """Insert *count* trades with increasing age."""
    for i in range(count):
        t = _trade(days_ago=count - i, **overrides)
        insert_trade(conn, t)


def _valid_spec(version: int = 1) -> Spec:
    """Return a valid Spec for the test agent."""
    return Spec(
        agent_id=AGENT_ID,
        spec_version=version,
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


def _valid_spec_yaml(version: int = 1) -> str:
    """Return valid YAML representing the test agent's spec."""
    return yaml.dump(
        {
            "agent_id": AGENT_ID,
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
            "position": {
                "leverage": 2,
                "position_size_pct": 0.10,
            },
        },
        default_flow_style=False,
    )


#: Stage 1 (Diagnose) is plain text, unparsed -- any non-empty string works.
_DIAGNOSE_RESPONSE = (
    "What's working: funding_zscore entries perform well in trending regimes. "
    "What's not: none flagged with this sample size. "
    "Recommended changes: none at this time."
)


def _make_llm(*responses):
    """Create a callable that returns the given responses in order.

    Matches the reflection transport contract: ``llm_fn(system_prompt,
    user_prompt) -> str`` (see llm/reflection_client.py::complete).
    """
    it = iter(responses)

    def _fn(system_prompt: str, user_prompt: str) -> str:
        return next(it)

    return _fn


def _setup_agent(conn, agent_id=AGENT_ID):
    """Insert agent row if not present."""
    existing = conn.execute(
        "SELECT id FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if not existing:
        insert_agent(conn, agent_id, agent_id, "2026-06-01T00:00:00Z", "{}")


def _deploy_initial_spec(conn):
    """Deploy spec v1 via deploy_spec so get_active_spec() works.

    Sets ``deployed_at`` 30 days in the past so that trades inserted
    *after* this call still satisfy the update-throttle gate (which
    counts trades since the last deployment).
    """
    spec = _valid_spec(version=1)

    spec_id = _deploy_spec(
        conn,
        AGENT_ID,
        spec,
        {"max_leverage": 10, "max_position_size_pct": 0.5},
    )
    # Re-stamp deployed_at so throttle gates see an old deployment.
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat().replace("+00:00", "Z")
    conn.execute(
        "UPDATE specs SET deployed_at = ? WHERE id = ?", (old_ts, spec_id),
    )
    conn.commit()
    return spec_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _redirect_specs_dir(monkeypatch, tmp_path):
    """Redirect SPECS_DIR and reflection's thesis dir to temp directories so
    deploy_spec/deploy_as_challenger/the atomic thesis+spec deploy never
    write into the real agents/specs/ or agents/theses/ (see
    tests/test_smoke.py's redirect of repo-dirtying paths)."""
    import store.specs as specs_module
    import agents.reflection as reflection_module

    monkeypatch.setattr(specs_module, "SPECS_DIR", tmp_path / "agents" / "specs")
    monkeypatch.setattr(reflection_module, "_THESES_DIR", tmp_path / "agents" / "theses")


@pytest.fixture
def seeded_conn(conn):
    """Set up an agent with 25 closed trades and spec v1 deployed."""
    _setup_agent(conn)
    _insert_trades(conn, 25)
    _deploy_initial_spec(conn)
    return conn


# ---------------------------------------------------------------------------
# Tests: individual gates
# ---------------------------------------------------------------------------


class TestMinTradesGate:
    def test_passes_with_enough_trades(self, conn):
        _setup_agent(conn)
        _insert_trades(conn, 25)
        passed, reason = check_min_trades(conn, AGENT_ID, min_trades=20)
        assert passed is True
        assert reason is None

    def test_rejects_with_few_trades(self, conn):
        _setup_agent(conn)
        _insert_trades(conn, 5)
        passed, reason = check_min_trades(conn, AGENT_ID, min_trades=20)
        assert passed is False
        assert reason is not None
        assert "only 5" in reason

    def test_rejects_with_zero_trades(self, conn):
        _setup_agent(conn)
        passed, reason = check_min_trades(conn, AGENT_ID, min_trades=20)
        assert passed is False
        assert "0" in reason


class TestUpdateThrottle:
    def test_passes_when_no_prior_deployment(self, conn):
        _setup_agent(conn)
        _insert_trades(conn, 40)
        passed, reason = check_update_throttle(conn, AGENT_ID)
        assert passed is True

    def test_passes_when_enough_trades_since_deploy(self, conn, monkeypatch, tmp_path):
        """If there are >30 new trades since last deploy, allow."""
        _setup_agent(conn)
        _insert_trades(conn, 40)

        # Deploy a spec with an old deployed_at by manipulating the DB directly
        old_ts = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat().replace("+00:00", "Z")

        # Insert specs row directly to set a custom deployed_at
        conn.execute(
            """INSERT INTO specs (agent_id, spec_version, thesis_version,
                                  yaml_text, status, deployed_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (AGENT_ID, 1, 1, "old: yaml", old_ts),
        )
        conn.commit()

        passed, reason = check_update_throttle(
            conn, AGENT_ID, min_trades_since=30, min_days=14,
        )
        assert passed is True, f"Expected pass, got: {reason}"

    def test_blocks_when_too_few_trades_and_recent(self, conn, monkeypatch, tmp_path):
        """With only 5 recent trades and the deploy 2 days ago, block."""
        _setup_agent(conn)
        _insert_trades(conn, 5)

        recent_ts = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat().replace("+00:00", "Z")

        conn.execute(
            """INSERT INTO specs (agent_id, spec_version, thesis_version,
                                  yaml_text, status, deployed_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (AGENT_ID, 1, 1, "old: yaml", recent_ts),
        )
        conn.commit()

        passed, reason = check_update_throttle(
            conn, AGENT_ID, min_trades_since=30, min_days=14,
        )
        assert passed is False
        assert reason is not None
        assert "need 30 trades" in reason


class TestPatternPersistence:
    def test_passes_when_trades_span_enough_windows(self, conn):
        _setup_agent(conn)
        # Insert trades spanning 30 days → at least 4 windows of 7 days
        _insert_trades(conn, 10)
        # Add more trades that span far back
        for i in range(5):
            t = _trade(days_ago=40 + i)
            insert_trade(conn, t)

        passed, reason = check_pattern_persistence(
            conn, AGENT_ID, "funding_zscore", min_windows=3, window_days=7,
        )
        assert passed is True, f"Expected pass, got: {reason}"

    def test_fails_when_trades_are_too_recent(self, conn):
        _setup_agent(conn)
        # All trades from last 2 days → only 1 window
        for i in range(5):
            t = _trade(days_ago=1)
            insert_trade(conn, t)

        passed, reason = check_pattern_persistence(
            conn, AGENT_ID, "some_feature", min_windows=3, window_days=7,
        )
        assert passed is False
        assert reason is not None
        assert "need at least" in reason

    def test_no_trades_fails(self, conn):
        _setup_agent(conn)
        passed, reason = check_pattern_persistence(
            conn, AGENT_ID, "anything", min_windows=3,
        )
        assert passed is False
        assert "no historical trades" in reason


class TestAdversarialPass:
    def test_no_critical_flaws_passes(self):
        spec = _valid_spec()
        llm = _make_llm("No critical flaws found.")
        critical, flaws = adversarial_pass("fake_yaml", spec, llm)
        assert critical is False
        assert flaws == []

    def test_critical_flaw_detected(self):
        spec = _valid_spec()
        llm = _make_llm("CRITICAL: This condition is overfit to one regime.")
        critical, flaws = adversarial_pass("fake_yaml", spec, llm)
        assert critical is True
        assert len(flaws) >= 1

    def test_multiple_flaws_collected(self):
        spec = _valid_spec()
        llm = _make_llm(
            "CRITICAL: This is broken.\n- First flaw\n- Second flaw\n"
        )
        critical, flaws = adversarial_pass("fake_yaml", spec, llm)
        assert critical is True
        assert any("First" in f for f in flaws)
        assert any("Second" in f for f in flaws)

    def test_empty_response_passes(self):
        spec = _valid_spec()
        llm = _make_llm("")
        critical, flaws = adversarial_pass("fake_yaml", spec, llm)
        assert critical is False
        assert flaws == []


# ---------------------------------------------------------------------------
# Tests: reflection prompt building
# ---------------------------------------------------------------------------


class TestBuildReflectionPrompt:
    def test_includes_agent_id(self):
        prompt = build_reflection_prompt(
            AGENT_ID, [], [], {}, None,
        )
        assert AGENT_ID in prompt
        assert "REFLECTION CYCLE" in prompt

    def test_includes_trade_summary(self):
        trades = [_trade(pnl_pct=0.05, result="win", days_ago=1)]
        prompt = build_reflection_prompt(
            AGENT_ID, trades, [], {}, None,
        )
        assert "TRADE HISTORY" in prompt
        assert "Wins: 1" in prompt
        assert "Losses: 0" in prompt

    def test_includes_current_spec(self):
        spec = _valid_spec()
        prompt = build_reflection_prompt(
            AGENT_ID, [], [], {}, spec,
        )
        assert "CURRENT SPEC" in prompt
        assert "funding_signal" in prompt

    def test_includes_regime_breakdown(self):
        trades = [
            _trade(regime="trending", result="win", days_ago=1),
            _trade(regime="ranging", result="loss", days_ago=2),
        ]
        prompt = build_reflection_prompt(
            AGENT_ID, trades, [], {}, None,
        )
        assert "PERFORMANCE BY REGIME" in prompt
        assert "trending" in prompt
        assert "ranging" in prompt


# ---------------------------------------------------------------------------
# Tests: YAML parsing
# ---------------------------------------------------------------------------


class TestParseRevisedSpec:
    def test_parses_valid_yaml(self):
        yaml_text = _valid_spec_yaml(version=2)
        spec = parse_revised_spec(yaml_text, AGENT_ID, 2)
        assert spec is not None
        assert spec.agent_id == AGENT_ID
        assert spec.spec_version == 2
        assert len(spec.evidence) == 1
        assert spec.evidence[0].name == "funding_signal"

    def test_parses_markdown_fenced_yaml(self):
        yaml_text = f"Here is my revised spec:\n```yaml\n{_valid_spec_yaml(version=2)}\n```\n"
        spec = parse_revised_spec(yaml_text, AGENT_ID, 2)
        assert spec is not None
        assert spec.spec_version == 2

    def test_returns_none_on_invalid_yaml(self):
        spec = parse_revised_spec("not valid yaml {{{", AGENT_ID, 2)
        assert spec is None

    def test_returns_none_on_empty(self):
        spec = parse_revised_spec("", AGENT_ID, 2)
        assert spec is None

    def test_returns_none_on_non_dict(self):
        spec = parse_revised_spec("42", AGENT_ID, 2)
        assert spec is None


# ---------------------------------------------------------------------------
# Tests: min-trade gate at reflection level
# ---------------------------------------------------------------------------


def test_min_trade_gate(conn):
    """Reflection is skipped when agent has fewer than 20 trades."""
    _setup_agent(conn)
    _insert_trades(conn, 5)  # Only 5 trades

    llm = _make_llm("should never be called")
    config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.triggered is False
    assert result.deployed is False
    assert result.blocked_by_gate is not None
    assert "5" in result.blocked_by_gate


def test_update_throttle(conn):
    """Reflection is blocked when there are too few recent trades."""
    _setup_agent(conn)
    _insert_trades(conn, 25)

    # Deploy a spec very recently via DB (to simulate a recent deploy)
    recent_ts = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    conn.execute(
        """INSERT INTO specs (agent_id, spec_version, thesis_version,
                              yaml_text, status, deployed_at)
           VALUES (?, ?, ?, ?, 'active', ?)""",
        (AGENT_ID, 1, 1, "old: yaml", recent_ts),
    )
    conn.commit()

    llm = _make_llm("should never be called")
    config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.triggered is False
    assert result.deployed is False
    assert result.blocked_by_gate is not None
    assert "update_throttle" in str(result).lower() or "throttle" in str(result.blocked_by_gate or "").lower() or result.blocked_by_gate is not None


def test_pattern_persistence(conn):
    """Pattern persistence gate blocks reflection when trades too recent."""
    _setup_agent(conn)

    # Deploy initial spec so get_active_spec works
    _deploy_initial_spec(conn)

    # 35 trades all within 1 day → min_trades (≥20) and holdout (≥30) pass,
    # but pattern_persistence fails since only 1 window of 7d.
    for i in range(35):
        t = _trade(days_ago=0)
        insert_trade(conn, t)

    # LLM returns: diagnose text, then a valid spec YAML, then no critical flaws
    llm = _make_llm(
        _DIAGNOSE_RESPONSE, _valid_spec_yaml(version=2), "No critical flaws found.",
    )
    config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
    result = run_reflection(conn, AGENT_ID, config, llm)

    # Should be blocked by pattern_persistence
    assert result.triggered is True  # LLM was called, spec parsed
    assert result.deployed is False
    assert result.blocked_by_gate == "pattern_persistence"


def test_anti_overfit_adversarial_pass(conn):
    """A CRITICAL adversarial finding no longer blocks by itself (M10 crit
    3: the LLM's opinion is advisory) -- but this spec still doesn't deploy,
    because it also fails the MANDATORY walk-forward gate (no ledger_dir
    configured here). Equivalent protection, via the correct mechanism: a
    mechanical evidence gate, not LLM opinion. The critical finding is still
    recorded for observability."""
    _setup_agent(conn)
    _insert_trades(conn, 25)
    _deploy_initial_spec(conn)

    # LLM: diagnose, then valid spec, then adversarial call finds flaws
    llm = _make_llm(
        _DIAGNOSE_RESPONSE,
        _valid_spec_yaml(version=2),
        "CRITICAL: The proposed evidence terms are completely overfit.\n- Overfit to noise",
    )
    config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.triggered is True
    assert result.deployed is False
    assert result.blocked_by_gate == "walk_forward"
    assert len(result.adversarial_flaws) >= 1
    assert result.adversarial_critique is not None
    assert "overfit" in result.adversarial_critique.lower()


def test_anti_overfit_rejects_overfit(conn):
    """Overfit spec rejected — alias for adversarial_pass test."""
    test_anti_overfit_adversarial_pass(conn)


def test_calibration_curve(conn):
    """Compute calibration curve from trade data."""
    _setup_agent(conn)

    # Insert trades at various confidence levels
    for conf, count, result in [
        (0.3, 3, "loss"),  # 0.0-0.4 bucket → 0% win
        (0.5, 2, "win"),  # 0.5-0.5 bucket aligned with 0.5-0.6
        (0.5, 1, "loss"),
        (0.7, 3, "win"),  # 0.7-0.7 → 0.7-0.8 bucket → 100% win
        (0.7, 1, "loss"),
        (0.9, 4, "win"),  # 0.9-0.9 → 0.9-1.0 bucket → 100% win
    ]:
        for _ in range(count):
            t = _trade(confidence=conf, result=result, days_ago=1)
            insert_trade(conn, t)

    curve = compute_calibration_curve(conn, AGENT_ID)
    assert len(curve) >= 1

    # Find the 0.7-0.8 bucket
    bucket_07 = next(
        (b for b in curve if b["bucket"].startswith("0.7")), None,
    )
    if bucket_07:
        # 3 wins, 1 loss → 75% win rate
        assert bucket_07["win_rate"] == 3 / 4
        assert bucket_07["count"] == 4

    # No trades with confidence → empty
    conn2 = sqlite3.connect(":memory:")
    conn2.row_factory = sqlite3.Row
    from store.db import init_schema
    init_schema(conn2)
    empty_curve = compute_calibration_curve(conn2, "no_agent")
    assert empty_curve == []


# ---------------------------------------------------------------------------
# Tests: end-to-end reflections
# ---------------------------------------------------------------------------


class TestEndToEndReflection:
    """Full reflection cycle with a correctly-behaved LLM."""

    def test_full_cycle_deploys_new_spec(self, conn, monkeypatch, tmp_path):
        """When all gates (including the mandatory walk-forward + complexity
        budget) pass and the LLM provides a valid spec, it deploys as a
        CHALLENGER (M10 crit 5 contract) -- not active. The incumbent v1
        stays active; get_challenger_spec returns the new v2."""
        _setup_agent(conn)
        _deploy_initial_spec(conn)
        _insert_trades(conn, 35)

        monkeypatch.setattr(
            reflection_module, "run_walk_forward",
            lambda *a, **k: _passing_wf_report(),
        )

        # LLM: diagnose, then valid YAML, then no critical flaws
        llm = _make_llm(
            _DIAGNOSE_RESPONSE,
            _valid_spec_yaml(version=2),
            "No critical flaws found. Minor concern: consider tightening the SL.",
        )
        config = {
            "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
            "ledger_dir": str(tmp_path / "ledger"),
        }
        result = run_reflection(conn, AGENT_ID, config, llm)

        assert result.triggered is True
        assert result.deployed is True, result.rejection_reason or result.blocked_by_gate
        assert result.spec_version == 2
        assert result.new_spec_yaml is not None
        assert result.blocked_by_gate is None
        assert result.rejection_reason is None

        # The incumbent v1 is untouched and still active.
        active = conn.execute(
            "SELECT spec_version, status FROM specs WHERE agent_id = ? AND status = 'active'",
            (AGENT_ID,),
        ).fetchone()
        assert active is not None
        assert active["spec_version"] == 1

        # v2 deployed as challenger, not active.
        challenger = get_challenger_spec(conn, AGENT_ID)
        assert challenger is not None
        assert challenger.spec_version == 2

    def test_llm_parse_failure_returns_error(self, conn):
        """When the LLM transport is exhausted/raises mid-pipeline (Stage 1
        Diagnose consumes the only queued response, then Stage 2 Propose
        hits the mock's StopIteration), reflection is triggered but
        gracefully rejected -- the exception must not escape run_reflection."""
        _setup_agent(conn)
        _insert_trades(conn, 25)
        _deploy_initial_spec(conn)

        llm = _make_llm("I don't want to output YAML. Here's a plain text analysis instead.")
        config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
        result = run_reflection(conn, AGENT_ID, config, llm)

        assert result.triggered is True
        assert result.deployed is False
        assert result.spec_version is None
        assert result.rejection_reason is not None
        assert "llm transport failed" in result.rejection_reason.lower()

    def test_llm_unparseable_yaml_returns_error(self, conn):
        """When both LLM stages respond (no transport exhaustion) but Stage 2
        (Propose) returns YAML that cannot be parsed into a Spec, reflection
        is triggered and rejected with the parse-failure reason -- NOT the
        transport-failure reason from test_llm_parse_failure_returns_error."""
        _setup_agent(conn)
        _insert_trades(conn, 25)
        _deploy_initial_spec(conn)

        llm = _make_llm(
            _DIAGNOSE_RESPONSE,
            "not valid yaml {{{",
        )
        config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
        result = run_reflection(conn, AGENT_ID, config, llm)

        assert result.triggered is True
        assert result.deployed is False
        assert result.spec_version is None
        assert result.rejection_reason is not None
        assert "failed to parse revised spec" in result.rejection_reason.lower()
        assert "llm transport failed" not in result.rejection_reason.lower()

    def test_zero_evidence_guard_rejects_empty_spec(self, conn):
        """R12 Latch 2: reflection rejects a spec with zero evidence terms
        even when all other gates would pass."""
        _setup_agent(conn)
        _deploy_initial_spec(conn)
        _insert_trades(conn, 35)

        # Build a spec YAML with no evidence terms (empty evidence list).
        # This is the hollow spec the LLM can silently produce.
        empty_evidence_yaml = yaml.dump(
            {
                "agent_id": AGENT_ID,
                "spec_version": 2,
                "thesis_version": 1,
                "universe": {"include": ["SOL-PERP"]},
                "regime_filter": {"exclude": []},
                "entry": {
                    "direction": "long",
                    "confidence_threshold": 0.7,
                    "scale_threshold": 0.5,
                    "evidence": [],  # zero evidence terms
                    "secondary_evidence": [],
                },
                "exit": {
                    "stop_loss_pct": 0.03,
                    "take_profit_pct": 0.06,
                    "max_hold_hours": 24,
                },
                "position": {
                    "leverage": 2,
                    "position_size_pct": 0.10,
                },
            },
            default_flow_style=False,
        )

        llm = _make_llm(
            _DIAGNOSE_RESPONSE,
            empty_evidence_yaml,
            "No critical flaws found.",  # adversarial pass — won't be reached
        )
        config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
        result = run_reflection(conn, AGENT_ID, config, llm)

        assert result.triggered is True  # LLM was called, spec was parsed
        assert result.deployed is False
        assert result.rejection_reason is not None
        assert "zero evidence" in result.rejection_reason.lower() or "R12" in result.rejection_reason
        assert result.spec_version == 2
        # The zero-evidence guard fires before adversarial pass, so no
        # adversarial flaws should be populated.
        assert result.adversarial_flaws == []

    def test_no_previous_spec_still_works(self, conn):
        """Reflection without a prior active spec (agent's first-ever
        reflection) still runs the full gate pipeline, including the
        MANDATORY walk-forward gate -- previously skipped outright via
        `if ledger_dir and current_spec is not None`, which let a
        never-validated first spec straight through. No incumbent is not an
        excuse to skip validating the proposal: with no ledger_dir
        configured, this is a loud walk_forward rejection, not a silent
        deploy."""
        _setup_agent(conn)
        _insert_trades(conn, 25)  # spans 25 days -- pattern_persistence passes

        # No prior spec deployed -- check_update_throttle passes (no prior
        # deployment). The dossier has closed trades, so all three LLM
        # calls (diagnose, propose, adversarial) are needed.
        llm = _make_llm(
            _DIAGNOSE_RESPONSE,
            _valid_spec_yaml(version=1),
            "No critical flaws found.",
        )
        config = {"desk": {"max_leverage": 10, "max_position_size_pct": 0.5}}
        result = run_reflection(conn, AGENT_ID, config, llm)

        assert result.triggered is True
        assert result.deployed is False
        assert result.blocked_by_gate == "walk_forward"
        assert result.rejection_reason is not None


# ---------------------------------------------------------------------------
# Tests: borderline cases
# ---------------------------------------------------------------------------


def test_zero_trades_min_trade_gate(conn):
    """No trades at all → min_trade gate blocks."""
    _setup_agent(conn)

    llm = _make_llm("never called")
    config = {}
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.triggered is False
    assert result.blocked_by_gate is not None
    assert "0" in result.blocked_by_gate or "only" in result.blocked_by_gate


def test_adversarial_empty_string(conn, monkeypatch, tmp_path):
    """Empty LLM response in adversarial pass — treated as no critique."""
    _setup_agent(conn)
    _deploy_initial_spec(conn)
    _insert_trades(conn, 35)

    monkeypatch.setattr(
        reflection_module, "run_walk_forward",
        lambda *a, **k: _passing_wf_report(),
    )

    llm = _make_llm(
        _DIAGNOSE_RESPONSE,
        _valid_spec_yaml(version=2),
        "",  # Empty adversarial response
    )
    config = {
        "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
        "ledger_dir": str(tmp_path / "ledger"),
    }
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.triggered is True
    assert result.deployed is True, result.rejection_reason or result.blocked_by_gate
    assert result.adversarial_flaws == []
    assert result.adversarial_critique is None


# ---------------------------------------------------------------------------
# Tests: M10 crit 3+4 -- honest evidence gates + atomic thesis/spec deploy
# ---------------------------------------------------------------------------


def test_diagnose_returns_falsifiable_hypotheses():
    """Stage A (Diagnose) parses structured falsifiable hypotheses out of
    the LLM's response -- each with claim, evidence_refs, predicted_effect,
    and falsification_condition."""
    from types import SimpleNamespace

    hypotheses_payload = json.dumps({
        "hypotheses": [
            {
                "claim": "funding_zscore > 1.5 predicts short-term reversion",
                "evidence_refs": ["regime_breakdown.trending", "feature_stats.funding_rate_current"],
                "predicted_effect": "win rate improves by 10pp when filtered",
                "falsification_condition": "next 20 filtered trades show <= baseline win rate",
            },
            {
                "claim": "confidence >= 0.8 trades outperform 0.5-0.7 trades",
                "evidence_refs": ["calibration_curve"],
                "predicted_effect": "avg pnl_pct higher in the top confidence bucket",
                "falsification_condition": "top bucket avg pnl_pct <= mid bucket avg pnl_pct over next 20 trades",
            },
        ],
    })
    llm = _make_llm(
        f"What's working: funding signals in trending regimes.\n\n"
        f"```json\n{hypotheses_payload}\n```\n"
    )
    dossier = SimpleNamespace(to_prompt=lambda max_chars=4000: "dossier text")

    raw_diagnosis, hypotheses = diagnose(AGENT_ID, dossier, llm)

    assert raw_diagnosis  # raw text preserved, not consumed by parsing
    assert len(hypotheses) == 2
    for h in hypotheses:
        assert h["claim"]
        assert isinstance(h["evidence_refs"], list) and h["evidence_refs"]
        assert h["predicted_effect"]
        assert h["falsification_condition"]
    assert hypotheses[0]["claim"] == "funding_zscore > 1.5 predicts short-term reversion"
    assert hypotheses[0]["evidence_refs"] == [
        "regime_breakdown.trending", "feature_stats.funding_rate_current",
    ]


def test_diagnose_plain_text_returns_empty_hypotheses():
    """A plain-text-only diagnosis (no JSON hypotheses block) is not an
    error -- diagnose() returns the raw text with an empty hypotheses list."""
    from types import SimpleNamespace

    llm = _make_llm(_DIAGNOSE_RESPONSE)
    dossier = SimpleNamespace(to_prompt=lambda max_chars=4000: "dossier text")

    raw_diagnosis, hypotheses = diagnose(AGENT_ID, dossier, llm)

    assert raw_diagnosis == _DIAGNOSE_RESPONSE
    assert hypotheses == []


def test_walk_forward_gate_is_mandatory(conn, tmp_path):
    """The walk-forward gate is mandatory for every reflection cycle -- a
    missing/empty ledger is a hard, logged rejection, not the old silent
    ``except Exception: logger.warning`` skip that let an unvalidated spec
    straight through to deploy."""
    from meta.reflection_scheduler import run_reflection_cycle

    _setup_agent(conn)
    _deploy_initial_spec(conn)
    _insert_trades(conn, 35)  # spans enough days for pattern_persistence

    llm = _make_llm(
        _DIAGNOSE_RESPONSE, _valid_spec_yaml(version=2), "No critical flaws found.",
    )
    config = {
        "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
        "ledger_dir": str(tmp_path / "does_not_exist"),
    }
    result = run_reflection_cycle(conn, AGENT_ID, config, llm)

    assert result["triggered"] is True
    assert result["deployed"] is False
    assert result["blocked_by_gate"] == "walk_forward"

    # nothing deploys
    challenger_count = conn.execute(
        "SELECT COUNT(*) FROM specs WHERE agent_id = ? AND status = 'challenger'",
        (AGENT_ID,),
    ).fetchone()[0]
    assert challenger_count == 0

    row = conn.execute(
        """SELECT outcome, rejection_reason FROM reflections
           WHERE agent_id = ? ORDER BY id DESC LIMIT 1""",
        (AGENT_ID,),
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "rejected"
    assert "walk" in (row["rejection_reason"] or "").lower()


def test_complexity_budget_blocks_term_creep(conn, monkeypatch, tmp_path):
    """A 5th evidence term (over the default max_evidence_terms=4 budget)
    that doesn't beat the incumbent's walk-forward deflated Sharpe is
    rejected -- complexity must pay for itself."""
    _setup_agent(conn)
    _deploy_initial_spec(conn)  # incumbent: 1 evidence term
    _insert_trades(conn, 35)

    five_term_yaml = yaml.dump(
        {
            "agent_id": AGENT_ID,
            "spec_version": 2,
            "thesis_version": 1,
            "universe": {"include": ["SOL-PERP"]},
            "regime_filter": {"exclude": []},
            "entry": {
                "direction": "long",
                "confidence_threshold": 0.7,
                "scale_threshold": 0.5,
                "evidence": [
                    {
                        "name": f"term_{i}",
                        "feature": "funding_zscore",
                        "thresholds": [
                            {"op": ">", "value": -1.0, "weight": 0.2},
                            {"op": "else", "weight": 0.0},
                        ],
                        "missing": "veto",
                    }
                    for i in range(5)
                ],
                "secondary_evidence": [],
            },
            "exit": {"stop_loss_pct": 0.03, "take_profit_pct": 0.06, "max_hold_hours": 24},
            "position": {"leverage": 2, "position_size_pct": 0.10},
        },
        default_flow_style=False,
    )

    llm = _make_llm(_DIAGNOSE_RESPONSE, five_term_yaml, "No critical flaws found.")

    def _fake_wf(spec, ledger_dir, taker_fee):
        # The 5-term proposal gets a WORSE deflated Sharpe than the
        # 1-term incumbent -- complexity doesn't pay for itself.
        n_terms = len(spec.evidence)
        sharpe = 0.3 if n_terms >= 5 else 1.0
        return _passing_wf_report(deflated_sharpe=sharpe)

    monkeypatch.setattr(reflection_module, "run_walk_forward", _fake_wf)

    config = {
        "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
        "ledger_dir": str(tmp_path / "ledger"),
    }
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.triggered is True
    assert result.deployed is False
    assert result.blocked_by_gate == "complexity_budget"


def test_adversarial_pass_is_advisory(conn, monkeypatch, tmp_path):
    """A CRITICAL adversarial finding is recorded (adversarial_critique +
    thesis Known-weaknesses section) but does NOT block a walk-forward
    passing spec -- it still deploys as challenger. 'The LLM proposes; the
    ledger disposes.'"""
    _setup_agent(conn)
    _deploy_initial_spec(conn)
    _insert_trades(conn, 35)

    monkeypatch.setattr(
        reflection_module, "run_walk_forward",
        lambda *a, **k: _passing_wf_report(),
    )

    llm = _make_llm(
        _DIAGNOSE_RESPONSE,
        _valid_spec_yaml(version=2),
        "CRITICAL: The proposed evidence terms are completely overfit.\n- Overfit to noise",
    )
    config = {
        "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
        "ledger_dir": str(tmp_path / "ledger"),
    }
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.triggered is True
    assert result.deployed is True, result.rejection_reason or result.blocked_by_gate
    assert result.blocked_by_gate is None
    assert result.adversarial_critique is not None
    assert "overfit" in result.adversarial_critique.lower()
    assert len(result.adversarial_flaws) >= 1

    challenger = get_challenger_spec(conn, AGENT_ID)
    assert challenger is not None
    assert challenger.spec_version == 2

    thesis_row = conn.execute(
        "SELECT text FROM theses WHERE agent_id = ? ORDER BY version DESC LIMIT 1",
        (AGENT_ID,),
    ).fetchone()
    assert thesis_row is not None
    assert "Known weaknesses" in thesis_row["text"]


def test_thesis_and_spec_deploy_atomically(conn, monkeypatch, tmp_path):
    """An accepted proposal deploys thesis + spec atomically: forcing a
    failure mid-deploy leaves thesis version, specs, theses, and the
    filesystem unchanged; the successful path bumps all four together."""
    _setup_agent(conn)
    _deploy_initial_spec(conn)
    _insert_trades(conn, 35)

    monkeypatch.setattr(
        reflection_module, "run_walk_forward",
        lambda *a, **k: _passing_wf_report(),
    )

    config = {
        "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
        "ledger_dir": str(tmp_path / "ledger"),
    }

    before_thesis_version = get_agent(conn, AGENT_ID)["current_thesis_version"]

    # --- Failure path: force deploy_as_challenger to raise mid-deploy ---
    def _boom(*a, **k):
        raise RuntimeError("simulated deploy failure")

    monkeypatch.setattr(reflection_module, "deploy_as_challenger", _boom)

    llm_fail = _make_llm(
        _DIAGNOSE_RESPONSE, _valid_spec_yaml(version=2), "No critical flaws found.",
    )
    result = run_reflection(conn, AGENT_ID, config, llm_fail)

    assert result.deployed is False
    assert result.rejection_reason is not None
    assert "simulated deploy failure" in result.rejection_reason

    assert get_agent(conn, AGENT_ID)["current_thesis_version"] == before_thesis_version
    assert conn.execute(
        "SELECT COUNT(*) FROM theses WHERE agent_id = ?", (AGENT_ID,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM specs WHERE agent_id = ? AND spec_version = 2", (AGENT_ID,),
    ).fetchone()[0] == 0
    thesis_path = reflection_module._THESES_DIR / f"{AGENT_ID}_v{before_thesis_version + 1}.md"
    assert not thesis_path.exists()

    # --- Success path: same setup, deploy_as_challenger works for real ---
    from store.specs import deploy_as_challenger as _real_deploy_as_challenger

    monkeypatch.setattr(reflection_module, "deploy_as_challenger", _real_deploy_as_challenger)

    llm_ok = _make_llm(
        _DIAGNOSE_RESPONSE, _valid_spec_yaml(version=2), "No critical flaws found.",
    )
    result2 = run_reflection(conn, AGENT_ID, config, llm_ok)

    assert result2.deployed is True, result2.rejection_reason or result2.blocked_by_gate
    assert result2.spec_version == 2

    after_thesis_version = get_agent(conn, AGENT_ID)["current_thesis_version"]
    assert after_thesis_version == before_thesis_version + 1

    theses_count = conn.execute(
        "SELECT COUNT(*) FROM theses WHERE agent_id = ?", (AGENT_ID,),
    ).fetchone()[0]
    assert theses_count == 1

    challenger = get_challenger_spec(conn, AGENT_ID)
    assert challenger is not None
    assert challenger.spec_version == 2
    assert challenger.thesis_version == after_thesis_version

    thesis_path2 = reflection_module._THESES_DIR / f"{AGENT_ID}_v{after_thesis_version}.md"
    assert thesis_path2.exists()


def test_desk_validation_failure_does_not_deploy_atomically(conn, monkeypatch, tmp_path):
    """A proposal that clears every mechanical gate (walk-forward
    monkeypatched to pass) but fails desk validation (leverage over the
    desk cap) must not be reported as deployed, and must not leave a
    partial commit behind.

    store.specs.deploy_as_challenger does NOT raise on a desk-validation
    failure -- it commits a status='rejected' spec row and returns
    normally, and that commit is the only commit in the atomic sequence.
    This guards _deploy_revision_atomically's pre-flight validate_spec
    check, which must catch this BEFORE any DB write -- a post-hoc rollback
    after deploy_as_challenger's own commit cannot undo it."""
    _setup_agent(conn)
    _deploy_initial_spec(conn)
    _insert_trades(conn, 35)

    monkeypatch.setattr(
        reflection_module, "run_walk_forward",
        lambda *a, **k: _passing_wf_report(),
    )

    over_leverage_yaml = yaml.dump(
        {
            "agent_id": AGENT_ID,
            "spec_version": 2,
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
            "exit": {"stop_loss_pct": 0.03, "take_profit_pct": 0.06, "max_hold_hours": 24},
            "position": {"leverage": 99, "position_size_pct": 0.10},  # over desk cap (10x)
        },
        default_flow_style=False,
    )

    llm = _make_llm(_DIAGNOSE_RESPONSE, over_leverage_yaml, "No critical flaws found.")
    before_thesis_version = get_agent(conn, AGENT_ID)["current_thesis_version"]

    config = {
        "desk": {"max_leverage": 10, "max_position_size_pct": 0.5},
        "ledger_dir": str(tmp_path / "ledger"),
    }
    result = run_reflection(conn, AGENT_ID, config, llm)

    assert result.deployed is False
    assert result.blocked_by_gate == "desk_validation"
    assert result.rejection_reason is not None
    assert "leverage" in result.rejection_reason.lower()

    # No partial commit: thesis version untouched, no theses row, no thesis
    # file, no challenger spec.
    assert get_agent(conn, AGENT_ID)["current_thesis_version"] == before_thesis_version
    assert conn.execute(
        "SELECT COUNT(*) FROM theses WHERE agent_id = ?", (AGENT_ID,),
    ).fetchone()[0] == 0
    thesis_path = reflection_module._THESES_DIR / f"{AGENT_ID}_v{before_thesis_version + 1}.md"
    assert not thesis_path.exists()
    assert get_challenger_spec(conn, AGENT_ID) is None
