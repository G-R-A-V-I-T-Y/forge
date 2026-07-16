"""tests/test_challenger.py — M10 challenger trial coverage.

Two things under test, both flagged by the T6 review as having zero
coverage:

1. store/specs.py's resolve_challenger(): promotion, rejection, and —
   finding 3 — that pre-trial incumbent decisions (logged before the
   challenger's deployed_at) never influence the outcome.
2. agents/decision_loop.py's shadow-challenger block (finding 2): the
   safety invariant that a challenger's decisions are logged (carrying
   challenger_spec_version) but NEVER reach the risk gate or the paper
   bridge — only the incumbent's decision is acted on. Matches
   FORGE_PROPOSAL's test_challenger_logs_without_trading intent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.dsl import EvidenceTerm, Spec, Threshold
from store.db import insert_agent
from store.specs import deploy_as_challenger, deploy_spec, resolve_challenger

AGENT_ID = "spec_agent"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


def _insert_decision(conn, agent_id: str, ts_iso: str, details: dict) -> int:
    cur = conn.execute(
        """INSERT INTO decisions
           (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, ts_iso, "enter", "test", json.dumps(details)),
    )
    conn.commit()
    return cur.lastrowid


def _insert_labeled_decision(
    conn,
    agent_id: str,
    ts_iso: str,
    details: dict,
    regret_pct: float,
    horizon: str = "4h",
) -> int:
    """Insert a decision AND its decision_labels row -- the production shape
    resolve_challenger's regret rewrite reads from (T8: mean labeled regret,
    not confidence parsed out of decision_details_json)."""
    decision_id = _insert_decision(conn, agent_id, ts_iso, details)
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


def _challenger_details(spec_version: int, incumbent_version: int) -> dict:
    """Production shape logged by agents/decision_loop.py's shadow-challenger
    block (ch_details in run_decision)."""
    return {
        "challenger_spec_version": spec_version,
        "challenger_confidence": 0.8,
        "challenger_action": "enter",
        "challenger_asset": "SOL-PERP",
        "challenger_evidence_strength": {"funding_term_challenger": 0.9},
        "incumbent_spec_version": incumbent_version,
    }


def _incumbent_enter_details() -> dict:
    """Production shape logged by agents/decision_loop.py's enter branch:
    {"order": str(response), "fill": str(fill)} -- NO confidence key. This
    is the exact shape that made the pre-T8 confidence-based
    resolve_challenger always read 0.0 for the incumbent on live data."""
    return {"order": "stub_order_repr", "fill": "stub_fill_repr"}


def _challenger_deployed_at(conn, agent_id: str, spec_version: int) -> str:
    row = conn.execute(
        """SELECT deployed_at FROM specs
           WHERE agent_id = ? AND status = 'challenger' AND spec_version = ?
           ORDER BY id DESC LIMIT 1""",
        (agent_id, spec_version),
    ).fetchone()
    assert row is not None
    return row["deployed_at"]


# ---------------------------------------------------------------------------
# Finding 3: resolve_challenger — direct coverage
# ---------------------------------------------------------------------------


class TestResolveChallenger:
    """T8 rewrite: resolve_challenger compares mean LABELED regret (joined
    from decision_labels at the canonical 4h horizon), not confidence
    parsed out of decision_details_json -- see resolve_challenger's
    docstring for the horizon-policy rationale. Fixtures use the real
    production row shapes (see _challenger_details / _incumbent_enter_details
    above), not the old {"confidence": ...} shape production never writes."""

    def test_promotion_path(self, conn):
        """Challenger's mean labeled regret is LOWER than the incumbent's
        over the trial window → promoted, active_spec_version flips,
        incumbent goes inactive."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        for i in range(2):
            _insert_labeled_decision(
                conn, AGENT_ID, _iso(base + timedelta(seconds=i + 1)),
                _challenger_details(2, 1), regret_pct=0.4,
            )
            _insert_labeled_decision(
                conn, AGENT_ID, _iso(base + timedelta(seconds=i + 1)),
                _incumbent_enter_details(), regret_pct=1.8,
            )

        result = resolve_challenger(conn, AGENT_ID)

        assert result["verdict"] == "promoted"
        assert result["challenger_version"] == 2
        assert result["incumbent_version"] == 1
        assert result["challenger_mean_regret"] == pytest.approx(0.4)
        assert result["incumbent_mean_regret"] == pytest.approx(1.8)
        assert result["challenger_labeled_decisions"] == 2
        assert result["incumbent_labeled_decisions"] == 2

        specs = {
            r["spec_version"]: r["status"]
            for r in conn.execute(
                "SELECT spec_version, status FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2] == "active"
        assert specs[1] == "inactive"

        agent = conn.execute(
            "SELECT active_spec_version FROM agents WHERE id = ?", (AGENT_ID,)
        ).fetchone()
        assert agent["active_spec_version"] == 2

    def test_rejection_path(self, conn):
        """Challenger's mean labeled regret is NOT lower than the
        incumbent's → rejected, incumbent stays active, challenger goes
        inactive."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _challenger_details(2, 1), regret_pct=2.5,
        )
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _incumbent_enter_details(), regret_pct=0.6,
        )

        result = resolve_challenger(conn, AGENT_ID)

        assert result["verdict"] == "rejected"
        assert result["challenger_mean_regret"] == pytest.approx(2.5)
        assert result["incumbent_mean_regret"] == pytest.approx(0.6)

        specs = {
            r["spec_version"]: r["status"]
            for r in conn.execute(
                "SELECT spec_version, status FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2] == "inactive"
        assert specs[1] == "active"

        agent = conn.execute(
            "SELECT active_spec_version FROM agents WHERE id = ?", (AGENT_ID,)
        ).fetchone()
        assert agent["active_spec_version"] == 1

    def test_no_challenger_returns_no_challenger_verdict(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1))

        result = resolve_challenger(conn, AGENT_ID)
        assert result == {"verdict": "no_challenger"}

    def test_not_resolvable_without_labels(self, conn):
        """T8 deliverable 1: zero labeled decisions on either side must
        never be promoted or rejected on zero evidence -- the spec rows are
        left completely untouched so the trial can keep running until the
        nightly labeling job catches up."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        # Decisions logged but never labeled (labeling job hasn't run yet).
        _insert_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _challenger_details(2, 1),
        )
        _insert_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _incumbent_enter_details(),
        )

        result = resolve_challenger(conn, AGENT_ID)

        assert result["verdict"] == "not_resolvable"
        assert result["challenger_mean_regret"] is None
        assert result["incumbent_mean_regret"] is None

        specs = {
            r["spec_version"]: r["status"]
            for r in conn.execute(
                "SELECT spec_version, status FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2] == "challenger"  # untouched -- not promoted or rejected
        assert specs[1] == "active"      # untouched

    # -----------------------------------------------------------------
    # T8 review Finding 2: force_close (window-expiry force-resolution)
    # -----------------------------------------------------------------

    def test_force_close_with_zero_evidence_closes_trial_terminally(self, conn):
        """force_close=True + zero evidence on one side -> verdict
        'expired_no_signal' and the challenger spec row is force-closed to
        'inactive' with a rejection_reason, instead of being left at
        'challenger' (which is what happens without force_close -- see
        test_not_resolvable_without_labels above)."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        _insert_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _challenger_details(2, 1),
        )  # unlabeled -- zero evidence

        result = resolve_challenger(conn, AGENT_ID, force_close=True)

        assert result["verdict"] == "expired_no_signal"
        assert result["challenger_mean_regret"] is None
        assert result["incumbent_mean_regret"] is None

        specs = {
            r["spec_version"]: (r["status"], r["rejection_reason"])
            for r in conn.execute(
                "SELECT spec_version, status, rejection_reason FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2][0] == "inactive"
        assert specs[2][1] is not None
        assert specs[1][0] == "active"  # incumbent untouched

    def test_force_close_does_not_alter_promotion_verdict(self, conn):
        """(c) force_close=True must have NO effect when both sides have
        evidence -- the ordinary promoted/rejected paths are unchanged."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        for i in range(2):
            _insert_labeled_decision(
                conn, AGENT_ID, _iso(base + timedelta(seconds=i + 1)),
                _challenger_details(2, 1), regret_pct=0.4,
            )
            _insert_labeled_decision(
                conn, AGENT_ID, _iso(base + timedelta(seconds=i + 1)),
                _incumbent_enter_details(), regret_pct=1.8,
            )

        result = resolve_challenger(conn, AGENT_ID, force_close=True)

        assert result["verdict"] == "promoted"  # NOT "expired_no_signal"
        assert result["challenger_mean_regret"] == pytest.approx(0.4)
        assert result["incumbent_mean_regret"] == pytest.approx(1.8)

        specs = {
            r["spec_version"]: r["status"]
            for r in conn.execute(
                "SELECT spec_version, status FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2] == "active"
        assert specs[1] == "inactive"

    def test_force_close_does_not_alter_rejection_verdict(self, conn):
        """(c) force_close=True must have NO effect on a genuine rejection
        (both sides labeled, challenger loses)."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _challenger_details(2, 1), regret_pct=2.5,
        )
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _incumbent_enter_details(), regret_pct=0.6,
        )

        result = resolve_challenger(conn, AGENT_ID, force_close=True)

        assert result["verdict"] == "rejected"  # NOT "expired_no_signal"
        specs = {
            r["spec_version"]: r["status"]
            for r in conn.execute(
                "SELECT spec_version, status FROM specs WHERE agent_id = ?",
                (AGENT_ID,),
            ).fetchall()
        }
        assert specs[2] == "inactive"
        assert specs[1] == "active"

    def test_pre_trial_incumbent_decisions_do_not_influence_outcome(self, conn):
        """T6 review finding 3: resolve_challenger must scope BOTH sides of
        the comparison to the challenger's trial window. A pre-trial
        incumbent decision (logged under a prior spec version, before this
        challenger was even deployed) must not be averaged in.

        Rigged so the two possible answers disagree: including the
        pre-trial near-zero-regret row would pull the incumbent average
        below the challenger's and flip the verdict to "rejected"; properly
        excluding it correctly promotes the challenger.
        """
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))

        # Pre-trial incumbent decision, logged long before the challenger
        # trial started.
        _insert_labeled_decision(
            conn, AGENT_ID, "2020-01-01T00:00:00Z",
            _incumbent_enter_details(), regret_pct=0.01,
        )

        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        # In-trial-window decisions.
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _challenger_details(2, 1), regret_pct=1.0,
        )
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            _incumbent_enter_details(), regret_pct=1.5,
        )

        result = resolve_challenger(conn, AGENT_ID)

        # Only the in-window incumbent decision (1.5) counted -- the
        # pre-trial 0.01-regret row was excluded.
        assert result["incumbent_labeled_decisions"] == 1
        assert result["incumbent_mean_regret"] == pytest.approx(1.5)
        assert result["verdict"] == "promoted"


# ---------------------------------------------------------------------------
# Finding 2: shadow-challenger block in agents/decision_loop.py
# ---------------------------------------------------------------------------


DECISION_AGENT_ID = "shadow_falcon"


def _decision_loop_heartbeat_packet() -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "timestamp": ts,
        "assets": {
            "SOL-PERP": {"price": 145.20, "funding": -0.005},
        },
        "cross_asset": {},
        "regime": {},
    }


def _decision_loop_config(heartbeat_path: str) -> dict:
    return {
        "universe": ["SOL-PERP"],
        "data_source": "stub",
        "desk": {
            "starting_balance": 50000.0,
            "max_leverage": 10,
            "max_position_size_pct": 0.20,
            "max_concurrent_positions": 3,
            "drawdown_kill_pct": 0.15,
            "heartbeat_path": heartbeat_path,
            "heartbeat_interval_seconds": 300,
        },
    }


@pytest.mark.asyncio
async def test_challenger_logs_without_trading(conn, tmp_path, monkeypatch):
    """The sole enforcement point for the M10 safety invariant: a
    challenger's shadow decision is logged with challenger_spec_version in
    decision_details_json, but never reaches the risk gate or the paper
    bridge. Only the incumbent's decision executes.

    Verified via spies on the REAL risk-gate function, the REAL
    RiskOfficer.entry_gate_status, and the REAL bridge_factory call site —
    not by mocking away the code under test.
    """
    from agents.decision_loop import run_decision
    from execution.paper_bridge import PaperBridge
    from market.heartbeat import write_heartbeat
    from market.provider import MarketProvider
    from store.db import insert_account_snapshot, get_trades

    insert_agent(
        conn, DECISION_AGENT_ID, DECISION_AGENT_ID, "2026-07-14T00:00:00Z",
        json.dumps({"compiled": True}),
    )
    insert_account_snapshot(conn, DECISION_AGENT_ID, "paper", 50000.0, 50000.0)

    # Incumbent: long, weight 0.6 on the funding evidence term -> confidence
    # 0.6 >= confidence_threshold(0.5) -> full-size enter (long).
    incumbent = Spec(
        agent_id=DECISION_AGENT_ID, spec_version=1, thesis_version=1,
        universe_include=["SOL-PERP"], regime_exclude=[], direction="long",
        confidence_threshold=0.5, scale_threshold=0.3,
        evidence=[
            EvidenceTerm(
                name="funding_term", feature="funding",
                thresholds=[
                    Threshold(op="<", weight=0.6, value=-0.001),
                    Threshold(op="else", weight=0.0),
                ],
                missing="skip",
            ),
        ],
        secondary_evidence=[], stop_loss_pct=0.03, take_profit_pct=0.06,
        max_hold_hours=72, leverage=3, position_size_pct=0.10,
    )
    # Challenger: short, weight 0.9 on the same feature -> confidence 0.9,
    # a DIFFERENT direction and higher confidence than the incumbent so the
    # test can tell the two decisions apart.
    challenger = Spec(
        agent_id=DECISION_AGENT_ID, spec_version=2, thesis_version=1,
        universe_include=["SOL-PERP"], regime_exclude=[], direction="short",
        confidence_threshold=0.5, scale_threshold=0.3,
        evidence=[
            EvidenceTerm(
                name="funding_term_challenger", feature="funding",
                thresholds=[
                    Threshold(op="<", weight=0.9, value=-0.001),
                    Threshold(op="else", weight=0.0),
                ],
                missing="skip",
            ),
        ],
        secondary_evidence=[], stop_loss_pct=0.03, take_profit_pct=0.06,
        max_hold_hours=72, leverage=3, position_size_pct=0.10,
    )

    deploy_spec(conn, DECISION_AGENT_ID, incumbent)
    deploy_as_challenger(conn, DECISION_AGENT_ID, challenger)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _decision_loop_heartbeat_packet())
    config = _decision_loop_config(heartbeat_path)

    # --- Spies on the real code path -----------------------------------
    from meta.risk_officer import RiskOfficer
    import agents.decision_loop as decision_loop_module

    gate_calls: list[str] = []
    orig_gate_status = RiskOfficer.entry_gate_status

    def spy_gate_status(self, agent_id, *a, **kw):
        gate_calls.append(agent_id)
        return orig_gate_status(self, agent_id, *a, **kw)

    monkeypatch.setattr(RiskOfficer, "entry_gate_status", spy_gate_status)

    validate_calls: list[int] = []
    orig_validate_order = decision_loop_module.validate_order

    def spy_validate_order(*args, **kwargs):
        validate_calls.append(1)
        return orig_validate_order(*args, **kwargs)

    monkeypatch.setattr(decision_loop_module, "validate_order", spy_validate_order)

    bridge_calls: list[str] = []

    def bridge_factory(agent_id, conn, provider):
        bridge_calls.append(agent_id)
        return PaperBridge(agent_id=agent_id, conn=conn, provider=provider, config=config)

    def sentinel_llm(sp, dp, **kw):
        raise AssertionError("compiled agent must not call the LLM")

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=DECISION_AGENT_ID,
            thesis_text="test thesis",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=sentinel_llm,
            bridge_factory=bridge_factory,
        )

    # (a) Only the incumbent's decision executes: a single long trade.
    assert result["action"] == "enter"
    trades = get_trades(conn, DECISION_AGENT_ID)
    assert len(trades) == 1
    assert trades[0]["direction"] == "long", (
        "the executed trade must be the incumbent's (long), never the "
        "challenger's (short)"
    )

    # (b) A challenger decisions row was logged, carrying
    # challenger_spec_version, distinct from the incumbent's own row.
    rows = [
        dict(r) for r in conn.execute(
            "SELECT decision_action, decision_reason, decision_details_json "
            "FROM decisions WHERE agent_id = ? ORDER BY id ASC",
            (DECISION_AGENT_ID,),
        ).fetchall()
    ]
    assert len(rows) == 2, f"expected challenger shadow log + incumbent log, got {rows}"

    challenger_row, incumbent_row = rows
    challenger_details = json.loads(challenger_row["decision_details_json"])
    assert challenger_details["challenger_spec_version"] == 2
    assert challenger_details["incumbent_spec_version"] == 1
    assert challenger_row["decision_action"] == "enter"
    assert "challenger/v2" in challenger_row["decision_reason"]

    incumbent_details = json.loads(incumbent_row["decision_details_json"])
    assert "challenger_spec_version" not in incumbent_details
    assert incumbent_row["decision_action"] == "enter"

    # (c) The challenger decision never invoked the risk gate or the
    # bridge -- exactly one call each, both attributable to the
    # incumbent's real entry (not two, which would mean the challenger
    # also reached execution).
    assert gate_calls == [DECISION_AGENT_ID]
    assert validate_calls == [1]
    assert bridge_calls == [DECISION_AGENT_ID]


# ---------------------------------------------------------------------------
# T8 review r2 Fix A: shadow decisions must be labelable by the REAL
# production pipeline. Before this fix, decision_loop.py's shadow block
# logged only challenger_* keys -- meta/labeling.py's extractors (enter
# needs "order", wait needs "candidate") returned None, so NO
# decision_labels row was ever written for a shadow decision and every
# live challenger trial ended not_resolvable/expired_no_signal, never
# promoted. These tests run the actual decision loop + the actual
# run_labeling_job over a synthetic candle ledger -- no direct
# decision_labels inserts anywhere.
# ---------------------------------------------------------------------------


async def _run_real_shadow_cycle(conn, tmp_path, challenger_spec: Spec) -> None:
    """Deploy incumbent v1 (long, enters on the stub heartbeat) plus
    *challenger_spec* as v2, then run the REAL decision loop once so it
    logs a genuine shadow decision row (and the incumbent's own row)."""
    from agents.decision_loop import run_decision
    from execution.paper_bridge import PaperBridge
    from market.heartbeat import write_heartbeat
    from market.provider import MarketProvider
    from store.db import insert_account_snapshot

    insert_agent(
        conn, DECISION_AGENT_ID, DECISION_AGENT_ID, "2026-07-14T00:00:00Z",
        json.dumps({"compiled": True}),
    )
    insert_account_snapshot(conn, DECISION_AGENT_ID, "paper", 50000.0, 50000.0)

    incumbent = Spec(
        agent_id=DECISION_AGENT_ID, spec_version=1, thesis_version=1,
        universe_include=["SOL-PERP"], regime_exclude=[], direction="long",
        confidence_threshold=0.5, scale_threshold=0.3,
        evidence=[
            EvidenceTerm(
                name="funding_term", feature="funding",
                thresholds=[
                    Threshold(op="<", weight=0.6, value=-0.001),
                    Threshold(op="else", weight=0.0),
                ],
                missing="skip",
            ),
        ],
        secondary_evidence=[], stop_loss_pct=0.03, take_profit_pct=0.06,
        max_hold_hours=72, leverage=3, position_size_pct=0.10,
    )
    deploy_spec(conn, DECISION_AGENT_ID, incumbent)
    deploy_as_challenger(conn, DECISION_AGENT_ID, challenger_spec)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _decision_loop_heartbeat_packet())
    config = _decision_loop_config(heartbeat_path)

    def sentinel_llm(sp, dp, **kw):
        raise AssertionError("compiled agent must not call the LLM")

    def bridge_factory(agent_id, conn_, provider_):
        return PaperBridge(
            agent_id=agent_id, conn=conn_, provider=provider_, config=config
        )

    provider = MarketProvider(config)
    async with provider:
        await run_decision(
            agent_id=DECISION_AGENT_ID,
            thesis_text="test thesis",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=sentinel_llm,
            bridge_factory=bridge_factory,
        )


def _decision_loop_challenger_spec(
    direction: str = "short",
    confidence_threshold: float = 0.5,
    scale_threshold: float = 0.3,
) -> Spec:
    return Spec(
        agent_id=DECISION_AGENT_ID, spec_version=2, thesis_version=1,
        universe_include=["SOL-PERP"], regime_exclude=[], direction=direction,
        confidence_threshold=confidence_threshold, scale_threshold=scale_threshold,
        evidence=[
            EvidenceTerm(
                name="funding_term_challenger", feature="funding",
                thresholds=[
                    Threshold(op="<", weight=0.9, value=-0.001),
                    Threshold(op="else", weight=0.0),
                ],
                missing="skip",
            ),
        ],
        secondary_evidence=[], stop_loss_pct=0.03, take_profit_pct=0.06,
        max_hold_hours=72, leverage=3, position_size_pct=0.10,
    )


def _backdate_cycle_and_write_candles(conn, tmp_path, hours_back: int = 25) -> Path:
    """Backdate this cycle's decision rows (and the challenger's
    deployed_at) *hours_back* into the past, then write a synthetic
    SOL-PERP 5m candle ledger from just before that point up to now --
    so run_labeling_job's head-minus-24h cutoff makes the decisions
    labelable at every horizon. Returns the ledger dir."""
    from store.ledger import append_ledger_record

    backdate = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    backdate_iso = _iso(backdate)
    conn.execute(
        "UPDATE decisions SET timestamp = ? WHERE agent_id = ?",
        (backdate_iso, DECISION_AGENT_ID),
    )
    conn.execute(
        """UPDATE specs SET deployed_at = ?
           WHERE agent_id = ? AND status = 'challenger'""",
        (_iso(backdate - timedelta(hours=1)), DECISION_AGENT_ID),
    )
    conn.commit()

    ledger_dir = tmp_path / "ledger"
    interval_ms = 5 * 60 * 1000
    start = backdate - timedelta(minutes=30)
    start_ms = int(start.timestamp() * 1000)
    n_candles = (hours_back * 60 + 60) // 5  # covers backdate-30m .. ~now+30m
    price = 145.20
    for i in range(n_candles):
        ts = start_ms + i * interval_ms
        o = price
        h = price * 1.002
        l = price * 0.999
        c = price * 1.0001
        append_ledger_record(
            "candles_5m",
            {"ts": ts, "asset": "SOL-PERP", "o": o, "h": h, "l": l, "c": c, "v": 1000.0},
            when=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
            ledger_dir=str(ledger_dir),
        )
        price = c
    return ledger_dir


def _shadow_decision_id(conn) -> int:
    row = conn.execute(
        """SELECT id FROM decisions
           WHERE agent_id = ? AND decision_details_json LIKE '%challenger_spec_version%'""",
        (DECISION_AGENT_ID,),
    ).fetchone()
    assert row is not None, "shadow decision row not logged"
    return row["id"]


@pytest.mark.asyncio
async def test_enter_shadow_labelable_by_production_pipeline(conn, tmp_path):
    """MANDATORY closing-the-hole test (Fix A): an enter-action shadow
    decision logged by the REAL decision loop is labeled by the REAL
    run_labeling_job (non-null regret_pct), and resolve_challenger then
    sees challenger evidence -- the trial is resolvable end-to-end on
    production-pipeline output alone."""
    from meta.labeling import run_labeling_job

    # Challenger confidence 0.9 >= threshold 0.5 -> enter-action shadow.
    await _run_real_shadow_cycle(
        conn, tmp_path, _decision_loop_challenger_spec(),
    )
    shadow_id = _shadow_decision_id(conn)
    ledger_dir = _backdate_cycle_and_write_candles(conn, tmp_path)

    result = run_labeling_job(conn, ledger_dir)
    assert result["errors"] == 0

    # (1) The shadow decision got a real decision_labels row with
    # non-null regret_pct at the canonical resolution horizon.
    label = conn.execute(
        """SELECT regret_pct FROM decision_labels
           WHERE decision_id = ? AND horizon = '4h'""",
        (shadow_id,),
    ).fetchone()
    assert label is not None, (
        "production labeling pipeline wrote no decision_labels row for the "
        "shadow decision -- challenger trials can never resolve on live data"
    )
    assert label["regret_pct"] is not None

    # (2) resolve_challenger sees challenger evidence produced entirely by
    # the production pipeline -- no direct decision_labels inserts.
    resolution = resolve_challenger(conn, DECISION_AGENT_ID)
    assert resolution["challenger_labeled_decisions"] > 0
    assert resolution["verdict"] in ("promoted", "rejected")


@pytest.mark.asyncio
async def test_wait_shadow_labelable_by_production_pipeline(conn, tmp_path):
    """Fix A, wait-action variant: a challenger whose confidence lands
    below its scale_threshold logs a wait-action shadow -- that row must
    also be labelable by the real pipeline."""
    from meta.labeling import run_labeling_job

    # Challenger confidence 0.9 < scale_threshold 0.95 -> wait-action shadow.
    await _run_real_shadow_cycle(
        conn, tmp_path,
        _decision_loop_challenger_spec(
            confidence_threshold=0.95, scale_threshold=0.95,
        ),
    )
    shadow_id = _shadow_decision_id(conn)
    shadow_action = conn.execute(
        "SELECT decision_action FROM decisions WHERE id = ?", (shadow_id,),
    ).fetchone()["decision_action"]
    assert shadow_action == "wait", "test premise: shadow must be a wait"

    ledger_dir = _backdate_cycle_and_write_candles(conn, tmp_path)
    result = run_labeling_job(conn, ledger_dir)
    assert result["errors"] == 0

    label = conn.execute(
        """SELECT regret_pct FROM decision_labels
           WHERE decision_id = ? AND horizon = '4h'""",
        (shadow_id,),
    ).fetchone()
    assert label is not None
    assert label["regret_pct"] is not None

    resolution = resolve_challenger(conn, DECISION_AGENT_ID)
    assert resolution["challenger_labeled_decisions"] > 0


def test_legacy_shadow_shape_skipped_not_crashed(conn, tmp_path):
    """Backward tolerance: shadow rows logged BEFORE the Fix A enrichment
    (challenger_* keys only, no "order"/"candidate") must be skipped by
    run_labeling_job -- zero labels, zero errors -- never crash the job."""
    from meta.labeling import run_labeling_job
    from store.ledger import append_ledger_record

    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")

    backdate = datetime.now(timezone.utc) - timedelta(hours=25)
    _insert_decision(
        conn, AGENT_ID, _iso(backdate),
        _challenger_details(2, 1),  # the exact pre-fix shadow shape
    )

    # Minimal candle ledger so the job has a head and the row is eligible.
    interval_ms = 5 * 60 * 1000
    start_ms = int((backdate - timedelta(minutes=30)).timestamp() * 1000)
    for i in range(26 * 12):
        ts = start_ms + i * interval_ms
        append_ledger_record(
            "candles_5m",
            {"ts": ts, "asset": "SOL-PERP", "o": 145.2, "h": 145.5,
             "l": 145.0, "c": 145.3, "v": 1000.0},
            when=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
            ledger_dir=str(tmp_path / "ledger"),
        )

    result = run_labeling_job(conn, tmp_path / "ledger")
    assert result["errors"] == 0
    n_labels = conn.execute("SELECT COUNT(*) FROM decision_labels").fetchone()[0]
    assert n_labels == 0


# ---------------------------------------------------------------------------
# T8 deliverable 7: named regression tests
# ---------------------------------------------------------------------------


def test_challenger_promotion_on_lower_regret(conn):
    """A challenger with lower mean labeled regret over the trial window
    becomes active; the incumbent is demoted to inactive."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
    deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
    deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

    deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
    base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

    for i in range(3):
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=i + 1)),
            _challenger_details(2, 1), regret_pct=0.5,
        )
        _insert_labeled_decision(
            conn, AGENT_ID, _iso(base + timedelta(minutes=i + 1)),
            _incumbent_enter_details(), regret_pct=2.0,
        )

    result = resolve_challenger(conn, AGENT_ID)

    assert result["verdict"] == "promoted"
    assert result["challenger_mean_regret"] == pytest.approx(0.5)
    assert result["incumbent_mean_regret"] == pytest.approx(2.0)

    specs = {
        r["spec_version"]: r["status"]
        for r in conn.execute(
            "SELECT spec_version, status FROM specs WHERE agent_id = ?",
            (AGENT_ID,),
        ).fetchall()
    }
    assert specs[2] == "active"
    assert specs[1] == "inactive"

    agent = conn.execute(
        "SELECT active_spec_version FROM agents WHERE id = ?", (AGENT_ID,)
    ).fetchone()
    assert agent["active_spec_version"] == 2


def test_challenger_rejection_resolves_hypotheses(conn):
    """T8 deliverable 4: a losing challenger trial marks the cycle's
    hypotheses falsified, with effect_observed recorded (not left null)."""
    from agents.reflection import register_hypotheses, resolve_hypotheses

    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
    deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))

    cur = conn.execute(
        "INSERT INTO reflections (agent_id, triggered_at) VALUES (?, ?)",
        (AGENT_ID, "2026-01-02T00:00:00Z"),
    )
    reflection_id = cur.lastrowid
    conn.commit()

    hyp_ids = register_hypotheses(conn, AGENT_ID, reflection_id, [
        {
            "claim": "funding term improves entries",
            "predicted_effect": "regret decreases",
            "falsification_condition": "challenger trial shows no improvement",
        },
    ])
    conn.execute(
        "UPDATE hypotheses SET status = 'challenger' WHERE id = ?", (hyp_ids[0],),
    )
    conn.commit()

    deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
    deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
    base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

    _insert_labeled_decision(
        conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
        _challenger_details(2, 1), regret_pct=3.0,
    )
    _insert_labeled_decision(
        conn, AGENT_ID, _iso(base + timedelta(minutes=1)),
        _incumbent_enter_details(), regret_pct=0.5,
    )

    challenger_result = resolve_challenger(conn, AGENT_ID)
    assert challenger_result["verdict"] == "rejected"

    n = resolve_hypotheses(conn, reflection_id, challenger_result)
    assert n == 1

    row = conn.execute(
        "SELECT status, effect_observed, resolved_at FROM hypotheses WHERE id = ?",
        (hyp_ids[0],),
    ).fetchone()
    assert row["status"] == "falsified"
    assert row["effect_observed"] == pytest.approx(0.5 - 3.0)
    assert row["resolved_at"] is not None


# ---------------------------------------------------------------------------
# T8 deliverable 2: scheduler-job wiring (ast-based, matches
# tests/test_labeling.py::TestForgeSchedulerAbsorption's convention of
# reading forge.py's source directly rather than importing the module).
# ---------------------------------------------------------------------------


class TestChallengerResolutionSchedulerWiring:
    def _forge_source(self) -> str:
        import pathlib

        forge_py = pathlib.Path(__file__).resolve().parents[1] / "forge.py"
        return forge_py.read_text(encoding="utf-8")

    def test_challenger_resolution_job_registered(self):
        source = self._forge_source()
        assert 'id="challenger_resolution"' in source, (
            "the M10 challenger-resolution scheduler job must be registered"
            " in forge.py"
        )

    def test_challenger_resolution_job_invokes_check_and_resolve(self):
        import ast

        source = self._forge_source()
        tree = ast.parse(source, filename="forge.py")

        job_node = None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_run_challenger_resolution_job"
            ):
                job_node = node
                break
        assert job_node is not None, (
            "_run_challenger_resolution_job not found in forge.py"
        )

        segment = ast.get_source_segment(source, job_node)
        assert segment is not None
        # T8 review r2 Fix B: the per-agent body (check trigger -> resolve
        # hypotheses -> record reflections.outcome unconditionally) was
        # extracted to agents/reflection.py::apply_challenger_resolution so
        # it is behaviorally testable without importing forge.py (which
        # needs apscheduler). The job must call that single entry point;
        # the check/resolve/outcome behavior itself is covered by
        # tests/test_hypotheses.py::TestApplyChallengerResolution.
        assert "apply_challenger_resolution(" in segment, (
            "expected the job to run the trigger + hypothesis resolution +"
            " outcome recording via"
            " agents.reflection.apply_challenger_resolution"
        )
