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
        assert "check_challenger_resolution(" in segment, (
            "expected the job to apply the min-decisions/max-days trigger"
            " via agents.reflection.check_challenger_resolution"
        )
        assert "resolve_hypotheses(" in segment, (
            "expected the job to resolve the cycle's hypotheses on"
            " challenger resolution"
        )
