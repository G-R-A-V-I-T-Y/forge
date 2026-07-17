"""tests/test_dossier.py — Tests for agents/dossier.py's M10 evidence dossier.

Covers:
- Top-regret decisions sourced from decision_labels (not legacy counterfactuals)
- Char-budget contract of Dossier.to_prompt()/truncate_prompt()
- Calibration curve computation (store/performance.py)
- build_dossier with synthetic DB data
"""
from __future__ import annotations

import json
import re

import pytest

from store.db import insert_agent

AGENT_ID = "test_agent"


# ── Helpers ──────────────────────────────────────────────────────────────


def _insert_decision(
    conn,
    agent_id: str,
    ts: str,
    action: str = "enter",
    reason: str = "test",
    asset: str | None = None,
    direction: str | None = None,
    cf_result: str | None = None,
    cf_better: int = 0,
) -> int:
    details = None
    if asset is not None:
        blob = {"asset": asset, "direction": direction, "entry_price": 100.0}
        key = "order" if action == "enter" else "candidate"
        details = json.dumps({key: str(blob)})

    cur = conn.execute(
        """INSERT INTO decisions
           (agent_id, timestamp, decision_action, decision_reason,
            decision_details_json, counterfactual_result, counterfactual_was_better)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, ts, action, reason, details, cf_result, cf_better),
    )
    conn.commit()
    return cur.lastrowid


def _insert_label(
    conn,
    decision_id: int,
    horizon: str,
    regret_pct: float,
    best_action: str = "wait",
) -> None:
    conn.execute(
        """INSERT INTO decision_labels
           (decision_id, horizon, fwd_return_pct, max_runup_pct,
            max_drawdown_pct, chosen_outcome_pct, best_action,
            best_outcome_pct, regret_pct, labeled_at)
           VALUES (?, ?, 0.0, 0.0, 0.0, 0.0, ?, 0.0, ?, ?)""",
        (decision_id, horizon, best_action, regret_pct, "2026-07-14T00:00:00Z"),
    )
    conn.commit()


def _insert_trade(conn, agent_id: str, ts: str, result: str, pnl: float,
                  asset: str = "BTC-PERP", regime: str = "trending",
                  confidence: float = 0.7) -> str:
    import uuid
    trade_id = str(uuid.uuid4())[:8]
    conn.execute(
        """INSERT INTO trades
           (id, agent_id, asset, direction, entry_price, stop_loss_price,
            take_profit_price, leverage, position_size_pct, notional_usd,
            entry_timestamp, exit_timestamp, exit_reason, pnl_pct, result,
            status, confidence, regime, voided)
           VALUES (?, ?, ?, 'long', 100.0, 97.0, 106.0, 2, 0.1, 1000.0,
                   ?, ?, 'sl', ?, ?, 'closed', ?, ?, 0)""",
        (trade_id, agent_id, asset, ts, ts, pnl, result, confidence, regime),
    )
    conn.commit()
    return trade_id


# ── Top regret decisions ────────────────────────────────────────────────


class TestTopRegretDecisions:
    def test_dossier_includes_top_regret_decisions(self, conn):
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")

        plain_ids = []
        for i in range(10):
            did = _insert_decision(
                conn, AGENT_ID, f"2026-07-01T00:{i:02d}:00Z",
                action="enter", asset="BTC-PERP", direction="long",
            )
            _insert_label(conn, did, "24h", regret_pct=100 - i * 5)
            plain_ids.append(did)

        multi_id = _insert_decision(
            conn, AGENT_ID, "2026-07-01T00:10:00Z",
            action="enter", asset="ETH-PERP", direction="short",
        )
        _insert_label(conn, multi_id, "1h", regret_pct=97, best_action="enter_short")
        _insert_label(conn, multi_id, "4h", regret_pct=10)
        _insert_label(conn, multi_id, "24h", regret_pct=5)

        legacy_id = _insert_decision(
            conn, AGENT_ID, "2026-07-01T00:11:00Z",
            action="wait", reason="legacy flagged",
            cf_result="would have won", cf_better=1,
        )

        dossier = build_dossier(conn, AGENT_ID)

        assert len(dossier.high_regret_decisions) == 10

        regrets = [d["regret_score"] for d in dossier.high_regret_decisions]
        assert regrets == sorted(regrets, reverse=True)
        assert regrets == [100, 97, 95, 90, 85, 80, 75, 70, 65, 60]

        decision_ids = [d["decision_id"] for d in dossier.high_regret_decisions]
        assert multi_id in decision_ids
        assert legacy_id not in decision_ids
        assert plain_ids[9] not in decision_ids

        top = dossier.high_regret_decisions[0]
        assert top["asset"] == "BTC-PERP"
        assert top["direction"] == "long"

        multi_entry = next(
            d for d in dossier.high_regret_decisions if d["decision_id"] == multi_id
        )
        assert multi_entry["horizon"] == "1h"
        assert multi_entry["best_action"] == "enter_short"
        assert multi_entry["asset"] == "ETH-PERP"

    def test_falls_back_to_legacy_when_agent_has_no_labels(self, conn):
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
        _insert_decision(
            conn, AGENT_ID, "2026-07-01T00:00:00Z",
            action="wait", reason="legacy flagged",
            cf_result="would have won", cf_better=1,
        )

        dossier = build_dossier(conn, AGENT_ID)

        assert len(dossier.high_regret_decisions) == 1
        assert dossier.high_regret_decisions[0]["decision_reason"] == "legacy flagged"


# ── Char budget ──────────────────────────────────────────────────────────


class TestCharBudget:
    def test_dossier_respects_char_budget(self):
        from agents.dossier import Dossier

        thesis_marker = "PRIORITY-THESIS-MARKER"
        records = "\n".join(f"RECORD-{i:04d}" for i in range(300))

        dossier = Dossier(
            agent_id=AGENT_ID,
            thesis_text=thesis_marker,
            spec_yaml="",
            closed_trades=[],
            calibration_curve={},
            high_regret_decisions=[],
            win_rate_by_regime={},
            profit_factor_by_regime={},
            feature_stats={},
            hypothesis_track_record=[],
            desk_memory_digest=records,
        )

        full_text = dossier.to_prompt(max_chars=1_000_000)
        assert len(full_text) > 500

        result = dossier.to_prompt(max_chars=500)

        assert len(result) <= 500
        assert len(result) < len(full_text)
        assert thesis_marker in result
        for token in re.findall(r"RECORD-\S*", result):
            assert re.fullmatch(r"RECORD-\d{4}", token), (
                f"truncation cut mid-record: {token!r}"
            )

    def test_truncate_prompt_short_text_passthrough(self):
        from agents.dossier import truncate_prompt

        text = "short text"
        assert truncate_prompt(text, 1000) == text

    def test_to_prompt_includes_char_count_header(self):
        from agents.dossier import Dossier

        dossier = Dossier(
            agent_id=AGENT_ID,
            thesis_text="test thesis",
            spec_yaml="direction: long",
            closed_trades=[],
            calibration_curve={},
            high_regret_decisions=[],
            win_rate_by_regime={},
            profit_factor_by_regime={},
            feature_stats={},
            hypothesis_track_record=[],
            desk_memory_digest="",
        )
        result = dossier.to_prompt(max_chars=5000)
        assert "EVIDENCE DOSSIER" in result
        assert AGENT_ID in result


# ── Calibration curve ────────────────────────────────────────────────────


class TestCalibrationCurve:
    def test_calibration_curve_buckets_by_confidence(self, conn):
        from store.performance import compute_calibration_curve

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")

        _insert_trade(conn, AGENT_ID, "2026-07-01T00:00:00Z", "win", 0.02,
                       confidence=0.3)
        _insert_trade(conn, AGENT_ID, "2026-07-01T01:00:00Z", "win", 0.03,
                       confidence=0.35)
        _insert_trade(conn, AGENT_ID, "2026-07-01T02:00:00Z", "loss", -0.01,
                       confidence=0.7)
        _insert_trade(conn, AGENT_ID, "2026-07-01T03:00:00Z", "win", 0.04,
                       confidence=0.75)
        _insert_trade(conn, AGENT_ID, "2026-07-01T04:00:00Z", "win", 0.01,
                       confidence=0.8)

        curve = compute_calibration_curve(conn, AGENT_ID)

        assert isinstance(curve, dict)
        assert "0.3-0.4" in curve
        assert curve["0.3-0.4"]["sample_count"] == 2
        assert curve["0.3-0.4"]["realized_wr"] == 1.0
        assert curve["0.3-0.4"]["confidence_mid"] == pytest.approx(0.35)

        assert "0.7-0.8" in curve
        assert curve["0.7-0.8"]["sample_count"] == 2
        assert curve["0.7-0.8"]["realized_wr"] == 0.5

        assert "0.8-0.9" in curve
        assert curve["0.8-0.9"]["sample_count"] == 1
        assert curve["0.8-0.9"]["realized_wr"] == 1.0

    def test_calibration_curve_empty_when_no_trades(self, conn):
        from store.performance import compute_calibration_curve

        insert_agent(conn, "empty_agent", "empty_agent",
                      "2026-06-29T00:00:00Z", "{}")
        curve = compute_calibration_curve(conn, "empty_agent")
        assert curve == {}

    def test_calibration_curve_in_dossier(self, conn):
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")

        for i in range(5):
            _insert_trade(conn, AGENT_ID, f"2026-07-01T0{i}:00:00Z",
                           "win" if i < 3 else "loss", 0.02 * (1 if i < 3 else -1),
                           confidence=0.5 + i * 0.1)

        dossier = build_dossier(conn, AGENT_ID)
        assert isinstance(dossier.calibration_curve, dict)
        assert len(dossier.calibration_curve) > 0


# ── Build dossier with synthetic data ────────────────────────────────────


class TestBuildDossier:
    def test_build_dossier_populates_all_fields(self, conn):
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")

        for i in range(5):
            regime = "trending" if i % 2 == 0 else "ranging"
            _insert_trade(conn, AGENT_ID, f"2026-07-01T0{i}:00:00Z",
                           "win" if i < 3 else "loss",
                           0.02 * (1 if i < 3 else -1), regime=regime,
                           confidence=0.6 + i * 0.05)

        dossier = build_dossier(conn, AGENT_ID)

        assert dossier.agent_id == AGENT_ID
        assert isinstance(dossier.thesis_text, str)
        assert isinstance(dossier.spec_yaml, str)
        assert isinstance(dossier.closed_trades, list)
        assert isinstance(dossier.calibration_curve, dict)
        assert isinstance(dossier.high_regret_decisions, list)
        assert isinstance(dossier.win_rate_by_regime, dict)
        assert isinstance(dossier.profit_factor_by_regime, dict)
        assert isinstance(dossier.feature_stats, dict)
        assert isinstance(dossier.hypothesis_track_record, list)
        assert isinstance(dossier.desk_memory_digest, str)

        assert len(dossier.closed_trades) == 5
        assert "trending" in dossier.win_rate_by_regime
        assert "ranging" in dossier.win_rate_by_regime
        assert dossier.win_rate_by_regime["trending"] == pytest.approx(0.6667, abs=0.001)
        assert dossier.win_rate_by_regime["ranging"] == pytest.approx(0.5)

    def test_build_dossier_empty_agent(self, conn):
        from agents.dossier import build_dossier

        insert_agent(conn, "empty", "empty", "2026-06-29T00:00:00Z", "{}")

        dossier = build_dossier(conn, "empty")

        assert dossier.agent_id == "empty"
        assert dossier.closed_trades == []
        assert dossier.calibration_curve == {}
        assert dossier.high_regret_decisions == []
        assert dossier.win_rate_by_regime == {}
        assert dossier.profit_factor_by_regime == {}
        assert dossier.feature_stats == {}
        assert dossier.hypothesis_track_record == []

    def test_to_prompt_renders_regime_breakdown(self, conn):
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")

        for i in range(6):
            _insert_trade(conn, AGENT_ID, f"2026-07-01T0{i}:00:00Z",
                           "win" if i % 2 == 0 else "loss",
                           0.02 * (1 if i % 2 == 0 else -1),
                           regime="trending")

        dossier = build_dossier(conn, AGENT_ID)
        prompt = dossier.to_prompt(max_chars=10000)

        assert "REGIME BREAKDOWN" in prompt
        assert "trending" in prompt

    def test_dossier_is_frozen(self, conn):
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
        dossier = build_dossier(conn, AGENT_ID)

        with pytest.raises(AttributeError):
            dossier.agent_id = "changed"
