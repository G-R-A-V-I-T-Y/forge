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


def _insert_decision(conn, agent_id: str, ts_iso: str, details: dict) -> None:
    conn.execute(
        """INSERT INTO decisions
           (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, ts_iso, "enter", "test", json.dumps(details)),
    )
    conn.commit()


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
    def test_promotion_path(self, conn):
        """Challenger's average confidence exceeds the incumbent's over the
        trial window → promoted, active_spec_version flips, incumbent goes
        inactive."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        for i in range(2):
            _insert_decision(
                conn, AGENT_ID, _iso(base + timedelta(seconds=i + 1)),
                {"challenger_spec_version": 2, "challenger_confidence": 0.9},
            )
            _insert_decision(
                conn, AGENT_ID, _iso(base + timedelta(seconds=i + 1)),
                {"confidence": 0.3},
            )

        result = resolve_challenger(conn, AGENT_ID)

        assert result["verdict"] == "promoted"
        assert result["challenger_version"] == 2
        assert result["incumbent_version"] == 1
        assert result["challenger_avg_confidence"] == pytest.approx(0.9)
        assert result["incumbent_avg_confidence"] == pytest.approx(0.3)
        assert result["challenger_decisions"] == 2
        assert result["incumbent_decisions"] == 2

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
        """Challenger's average confidence does NOT exceed the incumbent's
        → rejected, incumbent stays active, challenger goes inactive."""
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))
        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))

        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        _insert_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            {"challenger_spec_version": 2, "challenger_confidence": 0.2},
        )
        _insert_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            {"confidence": 0.5},
        )

        result = resolve_challenger(conn, AGENT_ID)

        assert result["verdict"] == "rejected"
        assert result["challenger_avg_confidence"] == pytest.approx(0.2)
        assert result["incumbent_avg_confidence"] == pytest.approx(0.5)

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

    def test_pre_trial_incumbent_decisions_do_not_influence_outcome(self, conn):
        """T6 review finding 3: resolve_challenger must scope BOTH sides of
        the comparison to the challenger's trial window. A pre-trial
        incumbent decision (logged under a prior spec version, before this
        challenger was even deployed) must not be averaged in.

        Rigged so the two possible answers disagree: including the
        pre-trial 0.99-confidence row would push the incumbent average
        above the challenger's and flip the verdict to "rejected"; properly
        excluding it correctly promotes the challenger.
        """
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        deploy_spec(conn, AGENT_ID, _spec(AGENT_ID, 1, direction="long"))

        # Pre-trial incumbent decision, logged long before the challenger
        # trial started.
        _insert_decision(
            conn, AGENT_ID, "2020-01-01T00:00:00Z", {"confidence": 0.99},
        )

        deploy_as_challenger(conn, AGENT_ID, _spec(AGENT_ID, 2, direction="short"))
        deployed_at = _challenger_deployed_at(conn, AGENT_ID, 2)
        base = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))

        # In-trial-window decisions.
        _insert_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            {"challenger_spec_version": 2, "challenger_confidence": 0.5},
        )
        _insert_decision(
            conn, AGENT_ID, _iso(base + timedelta(seconds=1)),
            {"confidence": 0.1},
        )

        result = resolve_challenger(conn, AGENT_ID)

        # Only the in-window incumbent decision (0.1) counted -- the
        # pre-trial 0.99 row was excluded.
        assert result["incumbent_decisions"] == 1
        assert result["incumbent_avg_confidence"] == pytest.approx(0.1)
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
