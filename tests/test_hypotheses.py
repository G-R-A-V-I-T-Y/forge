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
    get_hypothesis_track_record,
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

    def test_null_regret_pct_labeled_row_does_not_count(self, conn):
        """T8 review Finding 3 (MINOR): the trigger's labeled-decision count
        must agree with resolve_challenger's own regret-averaging loop,
        which skips any decision_labels row with regret_pct IS NULL.
        Before this fix the trigger counted such a row (only checking
        horizon, not regret_pct), so it could claim enough evidence and
        then immediately hit resolve_challenger's not_resolvable path on
        the very next call."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        base = _deployed_at_base(conn, AGENT_ID)

        # Labeled at the canonical horizon, but regret_pct itself is NULL
        # -- matches resolve_challenger's `if regret is None: continue`.
        cur = conn.execute(
            """INSERT INTO decisions
                   (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
               VALUES (?, ?, 'enter', 'test', ?)""",
            (
                AGENT_ID, _iso(base + timedelta(minutes=1)),
                json.dumps({"challenger_spec_version": 2, "incumbent_spec_version": 1}),
            ),
        )
        decision_id = cur.lastrowid
        conn.execute(
            """INSERT INTO decision_labels
                   (decision_id, horizon, fwd_return_pct, max_runup_pct,
                    max_drawdown_pct, chosen_outcome_pct, best_action,
                    best_outcome_pct, regret_pct, labeled_at)
               VALUES (?, '4h', 0, 0, 0, 0, 'enter_long', 0, NULL, ?)""",
            (decision_id, _iso(base + timedelta(minutes=1))),
        )
        conn.commit()

        result = check_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 1, "challenger_max_days": 7},
        )
        assert result["resolved"] is False
        assert "0/1" in result["reason"]

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
# T8 review Finding 2: window-expiry desync -- hypothesis status must never
# go terminal while the trial is still live. Three pinned behaviors:
#   (a) pre-expiry not_resolvable -> nothing changes anywhere
#   (b) post-expiry (max_days elapsed) not_resolvable -> trial closed
#       terminally + hypotheses inconclusive with effect_observed/resolved_at
#   (c) existing promoted/rejected paths unchanged (see test_challenger.py)
# ---------------------------------------------------------------------------


class TestWindowExpiryDesync:
    def test_pre_expiry_not_resolvable_leaves_everything_untouched(self, conn):
        """(a) The min_decisions threshold can fire on the challenger side
        alone while the incumbent side still has zero labeled decisions and
        max_days hasn't elapsed. That is NOT a window expiry -- the trial
        is genuinely still in progress, so check_challenger_resolution must
        report unresolved and touch neither the spec row nor any
        hypotheses."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        base = _deployed_at_base(conn, AGENT_ID)

        reflection_id = _insert_reflection(conn, AGENT_ID)
        hyp_ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
            {"claim": "c1", "predicted_effect": "e1"},
        ])
        conn.execute(
            "UPDATE hypotheses SET status = 'challenger' WHERE id = ?", (hyp_ids[0],),
        )
        conn.commit()

        # Challenger side clears min_decisions=2; incumbent side has zero
        # labeled decisions. deployed_at is "now" so days_elapsed ~ 0,
        # nowhere near max_days=7.
        for i in range(2):
            _insert_labeled_decision(
                conn, AGENT_ID, _iso(base + timedelta(minutes=i + 1)),
                {"challenger_spec_version": 2, "incumbent_spec_version": 1}, regret_pct=0.5,
            )

        result = check_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 2, "challenger_max_days": 7},
        )
        assert result["resolved"] is False

        specs = {
            r["spec_version"]: (r["status"], r["rejection_reason"])
            for r in conn.execute(
                "SELECT spec_version, status, rejection_reason FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2] == ("challenger", None)  # untouched
        assert specs[1][0] == "active"           # untouched

        hyp = conn.execute(
            "SELECT status, effect_observed, resolved_at FROM hypotheses WHERE id = ?",
            (hyp_ids[0],),
        ).fetchone()
        assert hyp["status"] == "challenger"     # untouched
        assert hyp["effect_observed"] is None
        assert hyp["resolved_at"] is None

    def test_post_expiry_not_resolvable_closes_trial_and_hypotheses(self, conn):
        """(b) max_days has elapsed with zero labeled decisions on the
        incumbent side -- check_challenger_resolution must force-close the
        trial (challenger spec row -> terminal 'inactive', with a
        rejection_reason recorded) AND report resolved=True so the caller
        (forge.py's hourly job) resolves the cycle's hypotheses to
        'inconclusive' with effect_observed/resolved_at set -- never
        leaving a terminal hypothesis desynced from a still-'challenger'
        spec row."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        # Backdate deployed_at well past max_days=7, with zero labeled
        # decisions on either side (labeling job never caught up). Must
        # stay timezone-aware ("Z" suffix) -- check_challenger_resolution
        # silently treats a naive/unparseable deployed_at as
        # days_elapsed=0.0 (caught ValueError/TypeError), which would
        # defeat this test's whole premise.
        base = _deployed_at_base(conn, AGENT_ID)
        old_ts = _iso(base - timedelta(days=10))
        conn.execute(
            "UPDATE specs SET deployed_at = ? WHERE agent_id = ? AND status = 'challenger'",
            (old_ts, AGENT_ID),
        )
        conn.commit()

        reflection_id = _insert_reflection(conn, AGENT_ID)
        hyp_ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
            {"claim": "c1", "predicted_effect": "e1"},
        ])
        conn.execute(
            "UPDATE hypotheses SET status = 'challenger' WHERE id = ?", (hyp_ids[0],),
        )
        conn.commit()

        result = check_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 20, "challenger_max_days": 7},
        )
        assert result["resolved"] is True
        assert result["verdict"] == "expired_no_signal"

        specs = {
            r["spec_version"]: (r["status"], r["rejection_reason"])
            for r in conn.execute(
                "SELECT spec_version, status, rejection_reason FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2][0] == "inactive"          # trial closed terminally
        assert specs[2][1] is not None            # reason recorded
        assert specs[1][0] == "active"            # incumbent untouched

        # Mirrors forge.py's hourly job: resolve the cycle's hypotheses
        # using this result.
        n = resolve_hypotheses(conn, reflection_id, result)
        assert n == 1
        hyp = conn.execute(
            "SELECT status, effect_observed, resolved_at FROM hypotheses WHERE id = ?",
            (hyp_ids[0],),
        ).fetchone()
        assert hyp["status"] == "inconclusive"
        assert hyp["effect_observed"] is None
        assert hyp["resolved_at"] is not None


# ---------------------------------------------------------------------------
# T8 review r2 Fix B: apply_challenger_resolution -- the per-agent body of
# forge.py's hourly job, extracted so it is behaviorally testable (forge.py
# itself imports apscheduler, unavailable in this env). Spec
# (FORGE_PROPOSAL.md:1183): "Either way the outcome lands in reflections AND
# resolves the cycle's hypotheses" -- TWO unconditional actions. Before this
# fix the reflections.outcome UPDATE sat inside the hypotheses loop, so a
# resolved trial whose cycle registered zero hypotheses (legacy path, or
# Stage A parsed none) never got its outcome recorded -- PENDING forever on
# the agent page.
# ---------------------------------------------------------------------------


class TestApplyChallengerResolution:
    def _deploy_trial_with_evidence(self, conn, verdict: str = "rejected") -> None:
        """Incumbent v1 + challenger v2 with enough labeled evidence that
        check_challenger_resolution resolves immediately (min_decisions=1)."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        base = _deployed_at_base(conn, AGENT_ID)

        ch_regret, inc_regret = (3.0, 0.5) if verdict == "rejected" else (0.5, 3.0)
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
            {"challenger_spec_version": 2, "incumbent_spec_version": 1},
            regret_pct=ch_regret,
        )
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
            {"order": "stub", "fill": "stub"}, regret_pct=inc_regret,
        )

    def test_zero_hypotheses_cycle_still_records_outcome(self, conn):
        """Fix B core: a resolved trial whose deploying cycle registered NO
        hypotheses must still get reflections.outcome written (to the most
        recent 'deployed' reflections row for the agent)."""
        from agents.reflection import apply_challenger_resolution

        self._deploy_trial_with_evidence(conn, verdict="rejected")

        # The reflections row that deployed this challenger -- outcome
        # 'deployed' as run_reflection_cycle writes it. NO hypotheses.
        cur = conn.execute(
            "INSERT INTO reflections (agent_id, triggered_at, outcome) VALUES (?, ?, 'deployed')",
            (AGENT_ID, "2026-01-02T00:00:00Z"),
        )
        reflection_id = cur.lastrowid
        conn.commit()

        result = apply_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 1, "challenger_max_days": 7},
        )
        assert result["resolved"] is True
        assert result["verdict"] == "rejected"

        row = conn.execute(
            "SELECT outcome FROM reflections WHERE id = ?", (reflection_id,),
        ).fetchone()
        assert row["outcome"] == "challenger_rejected", (
            "resolved trial with zero hypotheses must still record its "
            "outcome in the reflections row"
        )

    def test_with_hypotheses_resolves_both(self, conn):
        """Parity with the pre-extraction forge.py behavior: hypotheses
        resolved AND outcome written on the cycle that owns them."""
        from agents.reflection import apply_challenger_resolution

        self._deploy_trial_with_evidence(conn, verdict="promoted")

        cur = conn.execute(
            "INSERT INTO reflections (agent_id, triggered_at, outcome) VALUES (?, ?, 'deployed')",
            (AGENT_ID, "2026-01-02T00:00:00Z"),
        )
        reflection_id = cur.lastrowid
        conn.commit()
        hyp_ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
            {"claim": "c1", "predicted_effect": "e1"},
        ])
        conn.execute(
            "UPDATE hypotheses SET status = 'challenger' WHERE id = ?", (hyp_ids[0],),
        )
        conn.commit()

        result = apply_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 1, "challenger_max_days": 7},
        )
        assert result["resolved"] is True
        assert result["verdict"] == "promoted"

        hyp = conn.execute(
            "SELECT status, resolved_at FROM hypotheses WHERE id = ?", (hyp_ids[0],),
        ).fetchone()
        assert hyp["status"] == "validated"
        assert hyp["resolved_at"] is not None

        row = conn.execute(
            "SELECT outcome FROM reflections WHERE id = ?", (reflection_id,),
        ).fetchone()
        assert row["outcome"] == "challenger_promoted"

    def test_unresolved_trial_changes_nothing(self, conn):
        """Trial still in progress -> resolved=False, no outcome writes."""
        from agents.reflection import apply_challenger_resolution

        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        cur = conn.execute(
            "INSERT INTO reflections (agent_id, triggered_at, outcome) VALUES (?, ?, 'deployed')",
            (AGENT_ID, "2026-01-02T00:00:00Z"),
        )
        reflection_id = cur.lastrowid
        conn.commit()

        result = apply_challenger_resolution(
            conn, AGENT_ID, {"challenger_min_decisions": 20, "challenger_max_days": 7},
        )
        assert result["resolved"] is False

        row = conn.execute(
            "SELECT outcome FROM reflections WHERE id = ?", (reflection_id,),
        ).fetchone()
        assert row["outcome"] == "deployed"  # untouched


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


def test_get_hypothesis_track_record_returns_dossier_shape(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
    reflection_id = _insert_reflection(conn, AGENT_ID)
    ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
        {
            "claim": "funding drives reversion",
            "predicted_effect": "regret decreases",
            "feature": "funding",
            "direction": "down",
            "falsification_condition": "no improvement in challenger trial",
        },
    ])
    conn.execute(
        "UPDATE hypotheses SET status = 'challenger' WHERE id = ?", (ids[0],),
    )
    conn.commit()

    track = get_hypothesis_track_record(conn, AGENT_ID)
    assert len(track) == 1
    row = track[0]
    assert row["claim"] == "funding drives reversion"
    assert row["status"] == "challenger"
    assert row["feature"] == "funding"
    assert row["direction"] == "down"
    assert row["reflection_id"] == reflection_id
    assert row["resolved_at"] is None
    assert row["effect_observed"] is None

    assert get_hypothesis_track_record(conn, "nonexistent_agent") == []
