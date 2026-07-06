from datetime import datetime, timedelta, timezone

import pytest
from store.db import init_schema, insert_agent, insert_account_snapshot
from agents.decision_loop import run_decision
from agents.prompt_builder import build_decision_prompt
from market.heartbeat import write_heartbeat
from market.provider import MarketProvider
from llm.stub import decide
from execution.paper_bridge import PaperBridge

AGENT_ID = "jade_hawk"
THESIS = "Funding rate mean reversion: persistent negative funding signals short squeeze."


def _config(heartbeat_path: str) -> dict:
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


def _fresh_heartbeat_packet(timestamp: str | None = None) -> dict:
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "timestamp": ts,
        "assets": {
            "SOL-PERP": {
                "price": 145.20,
                "return_5m": 0.001,
                "return_24h": 0.05,
                "funding": -0.0042,
                "rsi": 61.3,
                "depth_imbalance": 0.12,
                "oi_zscore": 0.4,
            }
        },
        "cross_asset": {
            "market_breadth": 0.6,
            "leader": "SOL-PERP",
            "laggard": "SOL-PERP",
            "sector_strength": {"L1": 0.05},
        },
        "regime": {
            "regime_tag": "range_high_vol",
            "risk_on_score": 0.55,
            "trend_score": 0.2,
            "crypto_fear_index": 42,
        },
    }


def _bridge_factory(config):
    def bridge_factory(agent_id, conn, provider):
        return PaperBridge(agent_id=agent_id, conn=conn, provider=provider, config=config)
    return bridge_factory


STUB_MODEL_LABEL = "Test Stub Model"


def _stub_llm_fn(system_prompt, decision_prompt, **kwargs):
    """llm_fn now returns (decision_dict, model_display_name) — see
    llm/model_chain.py's decide(). Wraps llm.stub.decide() for tests that
    only care about the decision shape, not the fallback chain itself."""
    return decide(system_prompt, decision_prompt), STUB_MODEL_LABEL


@pytest.mark.asyncio
async def test_decision_loop_enter_creates_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_stub_llm_fn,
            bridge_factory=_bridge_factory(config),
        )

    assert result["action"] == "enter"
    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1
    # Fingerprint's categorical regime tag came from the heartbeat packet.
    row = conn.execute("SELECT regime FROM trades WHERE id = ?", (trades[0]["id"],)).fetchone()
    assert row["regime"] == "range_high_vol"

    # The consolidated trade-thumbprint (portfolio + cross_asset + regime +
    # full per-asset heartbeat fields) was captured onto the trade row.
    from store.query import get_trade
    full = get_trade(conn, trades[0]["id"], decode_ohlcv=True)
    mc = full["market_context_json"]
    assert mc is not None
    assert set(mc.keys()) == {"portfolio", "cross_asset", "regime", "asset"}
    assert mc["portfolio"]["cash"] == 50000.0
    assert mc["cross_asset"]["leader"] == "SOL-PERP"
    assert mc["regime"]["regime_tag"] == "range_high_vol"
    assert mc["asset"]["price"] == 145.20
    assert mc["asset"]["funding"] == -0.0042

    # model_used was recorded on the trade row and last_model_used on the
    # agent row from the (decision, model_used) tuple returned by llm_fn.
    assert full["model_used"] == STUB_MODEL_LABEL
    from store.db import get_agent
    assert get_agent(conn, AGENT_ID)["last_model_used"] == STUB_MODEL_LABEL


@pytest.mark.asyncio
async def test_decision_loop_risk_block_does_not_create_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def bad_llm(sys, prompt, **kwargs):
        return {
            "action": "enter",
            "asset": "SOL-PERP",
            "direction": "long",
            "entry_price": 145.20,
            "stop_loss_price": 143.00,
            "take_profit_price": 152.00,
            "leverage": 15,
            "position_size_pct": 0.10,
            "hypothesis": "test",
            "key_conditions_met": [],
            "key_conditions_missing": [],
            "confidence": 0.5,
            "expected_value": "test",
        }, "Test Bad Model"

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=bad_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result["action"] == "risk_blocked"
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []


@pytest.mark.asyncio
async def test_decision_loop_wait_does_not_create_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def wait_llm(sys, prompt, **kwargs):
        return {"action": "wait", "reason": "no setup fits thesis today"}, "Test Wait Model"

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=wait_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result["action"] == "wait"
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []

    # "wait" cycles still update last_model_used — the captain wants "most
    # recently used model", not "model used for the last trade".
    from store.db import get_agent
    assert get_agent(conn, AGENT_ID)["last_model_used"] == "Test Wait Model"


@pytest.mark.asyncio
async def test_decision_loop_error_action_propagates_and_records_no_model_available(conn, tmp_path):
    """When llm_fn (llm/model_chain.py's decide()) exhausts every tier, it
    returns ({"action": "error", "reason": "no model available"}, None).
    run_decision() must surface that as an explicit error result (not a
    generic "wait") and record the literal "no model available" sentinel
    on the agent row, distinct from NULL (no cycle run yet)."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def no_model_llm(sys, prompt, **kwargs):
        return {"action": "error", "reason": "no model available"}, None

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=no_model_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result == {"action": "error", "detail": "no model available"}
    from store.db import get_agent, get_trades
    assert get_agent(conn, AGENT_ID)["last_model_used"] == "no model available"
    assert get_trades(conn, AGENT_ID) == []


@pytest.mark.asyncio
async def test_decision_loop_missing_heartbeat_returns_wait(conn, tmp_path):
    """No heartbeat file at all -> immediate wait, no live-API fallback."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    heartbeat_path = str(tmp_path / "does_not_exist.json")
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_stub_llm_fn,
            bridge_factory=_bridge_factory(config),
        )

    assert result == {"action": "wait", "detail": "heartbeat unavailable or stale"}
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []


@pytest.mark.asyncio
async def test_decision_loop_stale_heartbeat_returns_wait(conn, tmp_path):
    """Heartbeat file exists but its timestamp is past 2x the interval -> wait."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet(timestamp=stale_ts))
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_stub_llm_fn,
            bridge_factory=_bridge_factory(config),
        )

    assert result == {"action": "wait", "detail": "heartbeat unavailable or stale"}


@pytest.mark.asyncio
async def test_decision_prompt_contains_heartbeat_sourced_data_and_cadence_notice(conn, tmp_path):
    """End-to-end: heartbeat-sourced fields actually reach the decision
    prompt text built by build_decision_prompt (not just the reader
    function in isolation), and the 5-minute cadence language is present."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    packet = _fresh_heartbeat_packet()
    prompt = await build_decision_prompt(
        AGENT_ID, THESIS, packet, conn, provider=None,
        starting_balance=50000.0, universe=["SOL-PERP"],
    )

    # Heartbeat-sourced per-asset field values reach the prompt text.
    assert "SOL-PERP" in prompt
    assert "145.2" in prompt  # price
    assert "range_high_vol" in prompt  # regime_tag
    assert "SOL-PERP" in prompt.split("Leader:")[1][:20]  # cross_asset leader

    # Hard cadence-awareness requirement from the captain.
    assert "every 5 minutes" in prompt
    assert "Do not assume intraday granularity finer than 5 minutes" in prompt
