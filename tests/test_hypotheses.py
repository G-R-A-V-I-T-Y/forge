"""tests/test_hypotheses.py — M10 hypothesis registry lifecycle coverage.

T8: wires the previously-dead functions in agents/reflection.py
(register_hypotheses, resolve_hypotheses, get_agent_hypothesis_history,
check_challenger_resolution) against production-shaped fixtures — the
hypotheses table + decision_labels-driven challenger resolution, not the
placeholder confidence comparison T8 replaces.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from agents.reflection import (
    check_challenger_resolution,
    get_agent_hypothesis_history,
    get_hypothesis_digest,
    register_hypotheses,
    resolve_hypotheses,
)
from backtest.dsl import EvidenceTerm, Spec, Threshold
from store.db import insert_agent
from store.specs import deploy_as_challenger, deploy_spec, resolve_challenger

AGENT_ID = "hypo_agent"


@pytest.fixture(autouse=True)
def _redirect_specs_dir(tmp_path, monkeypatch):
    """Never let a test write into the real agents/specs/ directory."""
    import store.specs as specs_module

    monkeypatch.setattr(specs_module, "SPECS_DIR", tmp_path / "specs")


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _spec(agent_id: str, spec_version: int, direction: str = "long") -> Spec:
    return Spec(
        agent_id=agent_id,
        spec_version=spec_version,
        thesis_version=1,
        universe_include=["SOL-PERP"],
        regime_exclude=[],
        direction=direction,
        confidence_threshold=0.5,
        scale_threshold=0.3,
        evidence=[
            EvidenceTerm(
                name="funding_term",
                feature="funding",
                thresholds=[
                    Threshold(op="<", weight=0.6, value=-0.001),
                    Threshold(op="else", weight=0.0),
                ],
                missing="skip",
            ),
        ],
        secondary_evidence=[],
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        max_hold_hours=72,
        leverage=3,
        position_size_pct=0.10,
    )


def _insert_reflection(conn, agent_id: str, triggered_at: str = "2026-07-01T00:00:00Z") -> int:
    cur = conn.execute(
        "INSERT INTO reflections (agent_id, triggered_at) VALUES (?, ?)",
        (agent_id, triggered_at),
    )
    conn.commit()
    return cur.lastrowid


def _insert_labeled_decision(
    conn, agent_id: str, ts_iso: str, details: dict, regret_pct: float, horizon: str = "4h",
) -> int:
    cur = conn.execute(
        """INSERT INTO decisions
               (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, 'enter', 'test', ?)""",
        (agent_id, ts_iso, json.dumps(details)),
    )
    conn.commit()
    decision_id = cur.lastrowid
    conn.execute(
        """INSERT INTO decision_labels
               (decision_id, horizon, fwd_return_pct, max_runup_pct,
                max_drawdown_pct, chosen_outcome_pct, best_action,
                best_outcome_pct, regret_pct, labeled_at)
           VALUES (?, ?, 0, 0, 0, 0, 'enter_long', 0, ?, ?)""",
        (decision_id, horizon, regret_pct, ts_iso),
    )
    conn.commit()
    return decision_id


def _deployed_at_base(conn, agent_id: str) -> datetime:
    row = conn.execute(
        """SELECT deployed_at FROM specs
           WHERE agent_id = ? AND status = 'challenger'
           ORDER BY id DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return datetime.fromisoformat(row["deployed_at"].replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# test_registry_roundtrip
# ---------------------------------------------------------------------------


class TestRegistryRoundtrip:
    def test_registry_roundtrip(self, conn):
        """proposed -> challenger -> validated transitions persist with
        timestamps and observed effects; re-resolving is a no-op."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))

        reflection_id = _insert_reflection(conn, AGENT_ID)

        ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
            {
                "claim": "negative funding predicts short-term reversion",
                "predicted_effect": "regret decreases when entering on funding<-0.001",
                "falsification_condition": "challenger trial shows no regret improvement",
            },
        ])
        assert len(ids) == 1
        hyp_id = ids[0]

        row = dict(conn.execute(
            "SELECT * FROM hypotheses WHERE id = ?", (hyp_id,),
        ).fetchone())
        assert row["status"] == "proposed"
        assert row["agent_id"] == AGENT_ID
        assert row["reflection_id"] == reflection_id
        assert row["resolved_at"] is None
        assert row["effect_observed"] is None

        history = get_agent_hypothesis_history(conn, AGENT_ID)
        assert len(history) == 1
        assert history[0]["claim"] == row["claim"]

        # Deploy as challenger -- registry status moves to 'challenger', the
        # transition run_reflection performs on a successful deploy.
        conn.execute("UPDATE hypotheses SET status = 'challenger' WHERE id = ?", (hyp_id,))
        conn.commit()
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        base = _deployed_at_base(conn, AGENT_ID)

        # Challenger beats incumbent -> promoted.
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
            {"challenger_spec_version": 2, "challenger_confidence": 0.8,
             "incumbent_spec_version": 1}, regret_pct=0.4,
        )
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
            {"order": "stub", "fill": "stub"}, regret_pct=2.0,
        )

        challenger_result = resolve_challenger(conn, AGENT_ID)
        assert challenger_result["verdict"] == "promoted"

        n = resolve_hypotheses(conn, reflection_id, challenger_result)
        assert n == 1

        resolved = dict(conn.execute(
            "SELECT * FROM hypotheses WHERE id = ?", (hyp_id,),
        ).fetchone())
        assert resolved["status"] == "validated"
        assert resolved["effect_observed"] == pytest.approx(2.0 - 0.4)
        assert resolved["resolved_at"] is not None

        # Re-resolving is a no-op -- already-resolved hypotheses are excluded.
        n2 = resolve_hypotheses(conn, reflection_id, challenger_result)
        assert n2 == 0

    def test_rejected_challenger_falsifies_all_cycle_hypotheses(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        reflection_id = _insert_reflection(conn, AGENT_ID)
        ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
            {"claim": "c1", "predicted_effect": "e1"},
            {"claim": "c2", "predicted_effect": "e2"},
        ])
        conn.executemany(
            "UPDATE hypotheses SET status = 'challenger' WHERE id = ?",
            [(i,) for i in ids],
        )
        conn.commit()
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        base = _deployed_at_base(conn, AGENT_ID)
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
            {"challenger_spec_version": 2, "incumbent_spec_version": 1}, regret_pct=3.0,
        )
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
            {"order": "stub", "fill": "stub"}, regret_pct=0.2,
        )

        result = resolve_challenger(conn, AGENT_ID)
        assert result["verdict"] == "rejected"

        n = resolve_hypotheses(conn, reflection_id, result)
        assert n == 2
        rows = conn.execute(
            "SELECT status, effect_observed FROM hypotheses WHERE reflection_id = ?",
            (reflection_id,),
        ).fetchall()
        for r in rows:
            assert r["status"] == "falsified"
            assert r["effect_observed"] == pytest.approx(0.2 - 3.0)

    def test_not_resolvable_verdict_marks_inconclusive(self, conn):
        """Window-expired-without-signal semantics: a 'not_resolvable'
        challenger verdict (zero labels) resolves the cycle's hypotheses to
        'inconclusive' rather than leaving them stuck forever."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        reflection_id = _insert_reflection(conn, AGENT_ID)
        ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
            {"claim": "c1", "predicted_effect": "e1"},
        ])
        conn.execute(
            "UPDATE hypotheses SET status = 'challenger' WHERE id = ?", (ids[0],),
        )
        conn.commit()

        n = resolve_hypotheses(conn, reflection_id, {"verdict": "not_resolvable"})
        assert n == 1
        row = conn.execute(
            "SELECT status, effect_observed, resolved_at FROM hypotheses WHERE id = ?",
            (ids[0],),
        ).fetchone()
        assert row["status"] == "inconclusive"
        assert row["effect_observed"] is None
        assert row["resolved_at"] is not None


# ---------------------------------------------------------------------------
# check_challenger_resolution: labeled-decision counting
# ---------------------------------------------------------------------------


class TestCheckChallengerResolution:
    def test_counts_only_labeled_decisions(self, conn):
        """M10 spec: challenger_min_decisions must count LABELED decisions,
        not raw shadow-log rows -- an unlabeled shadow row (the nightly
        labeling job hasn't caught up yet) must not count toward the
        threshold."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        base = _deployed_at_base(conn, AGENT_ID)

        # One labeled challenger decision...
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
            {"challenger_spec_version": 2, "incumbent_spec_version": 1}, regret_pct=1.0,
        )
        # ...and one unlabeled one (must not count toward min_decisions=2).
        conn.execute(
            """INSERT INTO decisions
                   (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
               VALUES (?, ?, 'enter', 'test', ?)""",
            (
                AGENT_ID, _iso(base + timedelta(minutes=2)),
                json.dumps({"challenger_spec_version": 2, "incumbent_spec_version": 1}),
            ),
        )
        conn.commit()

        result = check_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 2, "challenger_max_days": 7},
        )
        assert result["resolved"] is False
        assert "1/2" in result["reason"]

    def test_resolves_once_min_labeled_decisions_met(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        base = _deployed_at_base(conn, AGENT_ID)

        for i in range(2):
            _insert_labeled_decision(
                conn, AGENT_ID, _iso(base + timedelta(minutes=i + 1)),
                {"challenger_spec_version": 2, "incumbent_spec_version": 1}, regret_pct=0.5,
            )
            _insert_labeled_decision(
                conn, AGENT_ID, _iso(base + timedelta(minutes=i + 1)),
                {"order": "stub", "fill": "stub"}, regret_pct=2.0,
            )

        result = check_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 2, "challenger_max_days": 7},
        )
        assert result["resolved"] is True
        assert result["verdict"] == "promoted"

    def test_no_active_challenger_not_resolved(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))

        result = check_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 20, "challenger_max_days": 7},
        )
        assert result == {"resolved": False, "reason": "no active challenger"}


# ---------------------------------------------------------------------------
# get_hypothesis_digest — light coverage (wired for testability; no
# production call site is added by T8, see t8-report.md).
# ---------------------------------------------------------------------------


def test_get_hypothesis_digest_lists_recent_claims(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
    reflection_id = _insert_reflection(conn, AGENT_ID)
    register_hypotheses(conn, AGENT_ID, reflection_id, [
        {
            "claim": "funding drives reversion",
            "predicted_effect": "e",
            "feature": "funding",
            "direction": "down",
        },
    ])
    digest = get_hypothesis_digest(conn)
    assert "funding drives reversion" in digest
    assert "PROPOSED" in digest
