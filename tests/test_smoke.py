"""R10 smoke harness (trimmed) — the pre-run gate's final verification step.

Boots the real desk composition end-to-end with deterministic inputs and
asserts the full loop before the desk is trusted unattended:

  seeding (scripts/fresh_start.seed_desk + scripts/seed_benchmarks)
    → real heartbeat generation (market/heartbeat.generate_heartbeat over
      market/stub.StubMarket, written to disk + ledger partitions)
    → agent decision cycles (agents/decision_loop.run_decision) for a
      pure-LLM agent, a compiled agent, and both benchmark agents
    → risk gate → PaperBridge fill → open position
    → SL/TP wick reconciliation (store/positions.reconcile_positions)
      closing the trade with fees + funding from the shared cost model
    → wait-candidate capture → deterministic counterfactual replay
      (store/counterfactuals) filling the wait from ledger candles
    → account/ledger/state artifacts on disk.

The only patched-out internals are pure-external I/O that is not part of
the composition under test: the alternative.me Fear & Greed HTTP fetch,
and repo-dirtying module paths (OI history, spec/thesis dirs) which are
redirected to tmp so a smoke run never leaves the working tree dirty.

Run via:  python scripts/smoke_test.py
     or:  C:\\ProgramData\\Anaconda3\\python.exe -m pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.decision_loop import run_decision
from execution.paper_bridge import PaperBridge
from market.heartbeat import generate_heartbeat
from market.stub import StubMarket
from store.db import get_connection, init_schema

PROJECT_ROOT = Path(__file__).parent.parent

TAKER_FEE = 0.00035
STARTING_BALANCE = 50000.0


def _smoke_config(tmp_path: Path) -> dict:
    return {
        "universe": ["BTC-PERP", "ETH-PERP", "SOL-PERP"],
        "data_source": "stub",
        "desk": {
            "starting_balance": STARTING_BALANCE,
            "max_leverage": 10,
            "max_position_size_pct": 0.20,
            "max_concurrent_positions": 3,
            "drawdown_kill_pct": 0.15,
            "heartbeat_path": str(tmp_path / "heartbeat.json"),
            "heartbeat_interval_seconds": 300,
            "taker_fee": TAKER_FEE,
            "maker_fee": -0.00005,
        },
    }


@pytest.fixture
def smoke_env(tmp_path, monkeypatch):
    """Real file-backed DB + isolated side-effect paths + async fear-greed stub."""
    # External HTTP (alternative.me) is not part of the composition under test.
    async def _no_fear_greed():
        return None

    monkeypatch.setattr("market.heartbeat._fetch_fear_greed", _no_fear_greed)
    monkeypatch.setattr(
        "market.heartbeat.OI_HISTORY_PATH", str(tmp_path / "oi_history.json")
    )
    monkeypatch.setattr(
        "scripts.seed_benchmarks._THESES_DIR", tmp_path / "theses"
    )

    # Spec deploys write YAML next to the spec they were loaded from; give
    # the run a tmp copy of the real hand-compiled specs so reads are real
    # but writes never touch the repo.
    specs_dir = tmp_path / "specs"
    shutil.copytree(PROJECT_ROOT / "agents" / "specs", specs_dir)
    import scripts.fresh_start as fresh_start_module
    import store.specs as specs_module

    monkeypatch.setattr(specs_module, "SPECS_DIR", specs_dir)
    monkeypatch.setattr(fresh_start_module, "SPECS_DIR", specs_dir)

    conn = get_connection(str(tmp_path / "forge.db"))
    init_schema(conn)
    config = _smoke_config(tmp_path)
    yield conn, config, tmp_path
    conn.close()


def _bridge_factory(config):
    def factory(agent_id, conn, provider):
        return PaperBridge(agent_id=agent_id, conn=conn, provider=provider, config=config)

    return factory


def _sentinel_llm(*a, **k):
    raise AssertionError("this decision path must not call the LLM")


@pytest.mark.asyncio
async def test_full_cycle_open_and_close(smoke_env, monkeypatch):
    conn, config, tmp_path = smoke_env

    # ------------------------------------------------------------------
    # 1. Seeding — the single seed path, exactly as forge.py boots it.
    # ------------------------------------------------------------------
    from scripts.fresh_start import seed_desk
    from scripts.seed_benchmarks import seed_benchmark_agents

    created = seed_desk(conn, config)
    seed_benchmark_agents(conn, config)

    assert len(created) == 11  # 10 seed agents + sage_turtle

    roster = {
        r["id"]: json.loads(r["config_json"] or "{}")
        for r in conn.execute("SELECT id, config_json FROM agents").fetchall()
    }
    assert roster["iron_moth"].get("compiled") is True
    assert roster["silver_basin"].get("compiled") is True
    assert roster["jade_hawk"].get("compiled") is True
    assert roster["sage_turtle"].get("compiled") is True
    assert roster["copper_vane"].get("pinned_model"), "control arm must be pinned"
    assert roster["benchmark_btc_hold"].get("benchmark_type") == "btc_hold"
    assert roster["benchmark_random_walk"].get("benchmark_type") == "random_walk"

    spec_rows = conn.execute(
        "SELECT agent_id FROM specs WHERE status = 'active'"
    ).fetchall()
    assert {r["agent_id"] for r in spec_rows} >= {
        "iron_moth", "silver_basin", "jade_hawk", "sage_turtle",
    }, "every compiled agent must boot with a deployed spec"

    # ------------------------------------------------------------------
    # 2. Real heartbeat over the stub market.
    # ------------------------------------------------------------------
    provider = StubMarket()
    async with provider:
        packet = await generate_heartbeat(provider, config)

        heartbeat_path = Path(config["desk"]["heartbeat_path"])
        assert heartbeat_path.exists()
        assert packet["assets"]["SOL-PERP"]["price"] > 0

        import store.ledger as ledger_module

        ledger_dir = Path(ledger_module.LEDGER_DIR)
        month = f"{datetime.now(timezone.utc):%Y-%m}"
        assert (ledger_dir / "candles_5m" / f"{month}.jsonl").exists()

        # --------------------------------------------------------------
        # 3. Pure-LLM agent enters through gate + bridge.
        # --------------------------------------------------------------
        sol_price = packet["assets"]["SOL-PERP"]["price"]

        def enter_llm(sys_p, dec_p, **kw):
            return {
                "action": "enter",
                "asset": "SOL-PERP",
                "direction": "long",
                "entry_price": sol_price,
                "stop_loss_price": sol_price * 0.978,
                "take_profit_price": sol_price * 1.047,
                "leverage": 3,
                "position_size_pct": 0.10,
                "hypothesis": "smoke",
                "key_conditions_met": [],
                "key_conditions_missing": [],
                "confidence": 0.80,
                "evidence_strength": {"funding": 0.6},
                "expected_value": "smoke",
            }, "Smoke Stub Model"

        result = await run_decision(
            agent_id="copper_vane",
            thesis_text="smoke thesis",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=enter_llm,
            bridge_factory=_bridge_factory(config),
        )
        assert result["action"] == "enter", f"expected enter, got {result}"

        trade = conn.execute(
            "SELECT * FROM trades WHERE agent_id = 'copper_vane'"
        ).fetchone()
        assert trade["status"] == "open"
        # confidence 0.80 >= 0.70 → full size; true notional carries leverage.
        assert trade["true_notional"] == pytest.approx(
            STARTING_BALANCE * 0.10 * 3
        )

        # --------------------------------------------------------------
        # 4. Wait decision captures its counterfactual candidate.
        # --------------------------------------------------------------
        def wait_llm(sys_p, dec_p, **kw):
            return {
                "action": "wait",
                "reason": "smoke: just below threshold",
                "confidence": 0.55,
                "evidence_strength": {"funding": 0.2},
                "candidate": {
                    "asset": "SOL-PERP",
                    "direction": "long",
                    "entry_price": sol_price,
                    "stop_loss_price": sol_price * 0.97,
                    "take_profit_price": sol_price * 1.03,
                },
            }, "Smoke Stub Model"

        result = await run_decision(
            agent_id="onyx_heron",
            thesis_text="smoke thesis",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=wait_llm,
            bridge_factory=_bridge_factory(config),
        )
        assert result["action"] == "wait"

        wait_row = conn.execute(
            "SELECT id, decision_details_json FROM decisions "
            "WHERE agent_id = 'onyx_heron' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert json.loads(wait_row["decision_details_json"])["candidate"][
            "asset"
        ] == "SOL-PERP"

        # --------------------------------------------------------------
        # 5. Compiled agent runs the interpreter, never the LLM.
        # --------------------------------------------------------------
        result = await run_decision(
            agent_id="iron_moth",
            thesis_text="unused for compiled agents",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_sentinel_llm,
            bridge_factory=_bridge_factory(config),
        )
        assert result["action"] in ("enter", "wait"), f"unexpected: {result}"
        from store.db import get_agent

        assert get_agent(conn, "iron_moth")["last_model_used"].startswith("compiled/")

        # --------------------------------------------------------------
        # 6. Benchmarks trade: btc_hold enters exactly once.
        # --------------------------------------------------------------
        for _ in range(2):
            await run_decision(
                agent_id="benchmark_btc_hold",
                thesis_text="benchmark",
                config=config,
                conn=conn,
                provider=provider,
                llm_fn=_sentinel_llm,
                bridge_factory=_bridge_factory(config),
            )
        btc_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE agent_id = 'benchmark_btc_hold'"
        ).fetchone()[0]
        assert btc_trades == 1, "btc_hold must enter once and hold"

        import random as _random

        _random.seed(7)
        await run_decision(
            agent_id="benchmark_random_walk",
            thesis_text="benchmark",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_sentinel_llm,
            bridge_factory=_bridge_factory(config),
        )
        rw_decisions = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE agent_id = 'benchmark_random_walk'"
        ).fetchone()[0]
        assert rw_decisions >= 1

    # ------------------------------------------------------------------
    # 7. SL wick reconciliation closes copper_vane's trade with shared-
    #    cost-model fees and funding.
    # ------------------------------------------------------------------
    from store.positions import reconcile_positions

    pos = dict(
        conn.execute(
            "SELECT * FROM positions WHERE agent_id = 'copper_vane'"
        ).fetchone()
    )
    entry_price = pos["entry_price"]
    sl = pos["stop_loss_price"]
    true_notional = pos["true_notional"]

    await asyncio.sleep(0.3)  # ensure the funding event lands inside the window
    now_ms = int(time.time() * 1000)
    assets_data = {
        "SOL-PERP": {
            "price": entry_price,  # current price back INSIDE bounds --
            # only the wick crossed: exactly the phantom-survival case the
            # wick scan exists to catch.
            "candles_5m": [
                # [ts_ms, o, h, l, c, v] — low wicks through SL, closes back above.
                [now_ms, entry_price, entry_price * 1.001, sl * 0.995, entry_price, 100.0],
            ],
            "funding_history": [
                {"time": now_ms - 100, "fundingRate": 0.0001},
            ],
        }
    }

    closed = await reconcile_positions(conn, assets_data, None, config)
    assert closed == 1, "wick through SL must close the position"

    trade = dict(
        conn.execute(
            "SELECT * FROM trades WHERE agent_id = 'copper_vane'"
        ).fetchone()
    )
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(sl)
    # Fees: both sides on true notional via execution/costs.py.
    assert trade["fees_paid"] == pytest.approx(2 * TAKER_FEE * true_notional)
    # Funding: one event on true notional; long pays positive funding.
    assert trade["funding_paid"] == pytest.approx(true_notional * 0.0001)
    # Net PnL: notional × price move − fees − funding (no leverage² term).
    expected_pnl = (
        true_notional * (sl - entry_price) / entry_price
        - trade["fees_paid"]
        - trade["funding_paid"]
    )
    assert trade["pnl_usd"] == pytest.approx(expected_pnl, rel=1e-6)

    account = conn.execute(
        "SELECT balance FROM accounts WHERE agent_id = 'copper_vane' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert account["balance"] == pytest.approx(STARTING_BALANCE + expected_pnl)

    # ------------------------------------------------------------------
    # 8. Deterministic counterfactual replay fills the captured wait.
    # ------------------------------------------------------------------
    import store.counterfactuals as cf
    import store.ledger as ledger_module

    monkeypatch.setattr(cf, "MIN_WAIT_AGE_HOURS", 0)

    # Future candles in the REAL ledger format (ISO ts): TP crossed.
    sol_price = json.loads(wait_row["decision_details_json"])["candidate"]["entry_price"]
    tp = sol_price * 1.03
    part = Path(ledger_module.LEDGER_DIR) / "candles_5m" / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
    with open(part, "a", encoding="utf-8") as f:
        for i in range(1, 7):
            c_dt = datetime.now(timezone.utc) + timedelta(minutes=5 * i)
            px = sol_price if i < 3 else tp * 1.01
            f.write(json.dumps({
                "ts": c_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "asset": "SOL-PERP",
                "o": px, "h": px * 1.002, "l": px * 0.998, "c": px, "v": 10.0,
            }) + "\n")

    summary = cf.run_counterfactual_replay(conn, config, ledger_module.LEDGER_DIR)
    assert summary["errors"] == 0
    assert summary["filled"] >= 1

    cf_row = conn.execute(
        "SELECT counterfactual_result, counterfactual_was_better FROM decisions WHERE id = ?",
        (wait_row["id"],),
    ).fetchone()
    assert cf_row["counterfactual_result"] is not None, (
        "the captured wait candidate must be counterfactually scored"
    )
    assert json.loads(cf_row["counterfactual_result"])["reason"] == "take_profit"
    assert cf_row["counterfactual_was_better"] == 1

    # ------------------------------------------------------------------
    # 9. State snapshot reflects the run.
    # ------------------------------------------------------------------
    import store.state_snapshot as snapshot_module
    from store.state_snapshot import write_current_state

    write_current_state(conn)
    state_path = Path(snapshot_module.DEFAULT_STATE_PATH)
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("agents"), "state snapshot must carry the roster"
