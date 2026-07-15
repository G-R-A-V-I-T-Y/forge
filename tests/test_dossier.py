"""tests/test_dossier.py — Tests for agents/dossier.py's M10 evidence dossier.

Focused on the two M10-criterion behaviors: real-label-sourced top-regret
decisions (not the old counterfactual column) and the char-budget contract
of Dossier.to_prompt()/truncate_prompt().
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


# ── Top regret decisions ────────────────────────────────────────────────


class TestTopRegretDecisions:
    def test_dossier_includes_top_regret_decisions(self, conn):
        """Top-10 highest-regret decisions come from decision_labels (max
        regret across horizons), not the legacy counterfactual column."""
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")

        # Ten single-horizon decisions with descending regret 100..55.
        plain_ids = []
        for i in range(10):
            did = _insert_decision(
                conn, AGENT_ID, f"2026-07-01T00:{i:02d}:00Z",
                action="enter", asset="BTC-PERP", direction="long",
            )
            _insert_label(conn, did, "24h", regret_pct=100 - i * 5)
            plain_ids.append(did)

        # One decision whose 24h regret is low but 1h regret is high —
        # proves the max-across-horizons policy (not "24h only").
        multi_id = _insert_decision(
            conn, AGENT_ID, "2026-07-01T00:10:00Z",
            action="enter", asset="ETH-PERP", direction="short",
        )
        _insert_label(conn, multi_id, "1h", regret_pct=97, best_action="enter_short")
        _insert_label(conn, multi_id, "4h", regret_pct=10)
        _insert_label(conn, multi_id, "24h", regret_pct=5)

        # A legacy-flagged decision with NO decision_labels row at all.
        # Once any label exists for this agent, labeled data always wins —
        # this decision must never appear even though the old column
        # flags it as a missed opportunity.
        legacy_id = _insert_decision(
            conn, AGENT_ID, "2026-07-01T00:11:00Z",
            action="wait", reason="legacy flagged",
            cf_result="would have won", cf_better=1,
        )

        dossier = build_dossier(conn, AGENT_ID)

        assert len(dossier.top_regret_decisions) == 10

        regrets = [d["regret_score"] for d in dossier.top_regret_decisions]
        assert regrets == sorted(regrets, reverse=True)
        # Expected: 100 (d0), 97 (multi via 1h), 95 (d1), 90, 85, 80, 75, 70, 65, 60
        assert regrets == [100, 97, 95, 90, 85, 80, 75, 70, 65, 60]

        decision_ids = [d["decision_id"] for d in dossier.top_regret_decisions]
        assert multi_id in decision_ids
        assert legacy_id not in decision_ids
        assert plain_ids[9] not in decision_ids  # regret=55, 11th place, excluded

        # Market context present for the enter decision.
        top = dossier.top_regret_decisions[0]
        assert top["asset"] == "BTC-PERP"
        assert top["direction"] == "long"

        # The 1h-driven entry carries its horizon and best_action.
        multi_entry = next(
            d for d in dossier.top_regret_decisions if d["decision_id"] == multi_id
        )
        assert multi_entry["horizon"] == "1h"
        assert multi_entry["best_action"] == "enter_short"
        assert multi_entry["asset"] == "ETH-PERP"

    def test_falls_back_to_legacy_when_agent_has_no_labels(self, conn):
        """An agent with zero decision_labels rows still gets regret
        decisions, sourced from the legacy counterfactual column."""
        from agents.dossier import build_dossier

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
        _insert_decision(
            conn, AGENT_ID, "2026-07-01T00:00:00Z",
            action="wait", reason="legacy flagged",
            cf_result="would have won", cf_better=1,
        )

        dossier = build_dossier(conn, AGENT_ID)

        assert len(dossier.top_regret_decisions) == 1
        assert dossier.top_regret_decisions[0]["decision_reason"] == "legacy flagged"


# ── Char budget ──────────────────────────────────────────────────────────


class TestCharBudget:
    def test_dossier_respects_char_budget(self):
        """to_prompt(max_chars) truncates by section priority and never
        mid-record: the lowest-priority section (desk digest) is dropped/
        truncated first, and every full "record" line that does appear is
        complete -- never a partial fragment of one."""
        from agents.dossier import Dossier

        thesis_marker = "PRIORITY-THESIS-MARKER"
        records = "\n".join(f"RECORD-{i:04d}" for i in range(300))

        dossier = Dossier(
            agent_id=AGENT_ID,
            thesis_text=thesis_marker,
            active_spec_yaml="",
            closed_trades=[],
            calibration_curve=[],
            top_regret_decisions=[],
            regime_breakdown=[],
            feature_stats=[],
            hypothesis_history=[],
            desk_digest=records,
        )

        full_text = dossier.to_prompt(max_chars=1_000_000)
        assert len(full_text) > 500  # sanity: our fixture is actually large

        result = dossier.to_prompt(max_chars=500)

        # Never exceeds the budget.
        assert len(result) <= 500
        # Truncation actually happened (content dropped).
        assert len(result) < len(full_text)
        # Priority preserved: the higher-priority THESIS section survives.
        assert thesis_marker in result
        # Never mid-record: every "RECORD-" token present is a complete,
        # well-formed record -- never a partial cut like "RECORD-00".
        for token in re.findall(r"RECORD-\S*", result):
            assert re.fullmatch(r"RECORD-\d{4}", token), (
                f"truncation cut mid-record: {token!r}"
            )

    def test_truncate_prompt_short_text_passthrough(self):
        """Text already under the budget is returned unchanged."""
        from agents.dossier import truncate_prompt

        text = "short text"
        assert truncate_prompt(text, 1000) == text
