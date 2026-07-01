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


def bridge_factory(agent_id, conn, provider):
    return PaperBridge(agent_id=agent_id, conn=conn, provider=provider)


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
            llm_fn=decide,
            bridge_factory=bridge_factory,
        )

    assert result["action"] == "enter"
    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1
    # Fingerprint's categorical regime tag came from the heartbeat packet.
    row = conn.execute("SELECT regime FROM trades WHERE id = ?", (trades[0]["id"],)).fetchone()
    assert row["regime"] == "range_high_vol"


@pytest.mark.asyncio
async def test_decision_loop_risk_block_does_not_create_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def bad_llm(sys, prompt):
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
        }

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
            bridge_factory=bridge_factory,
        )

    assert result["action"] == "risk_blocked"
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []


@pytest.mark.asyncio
async def test_decision_loop_wait_does_not_create_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def wait_llm(sys, prompt):
        return {"action": "wait", "reason": "no setup fits thesis today"}

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
            bridge_factory=bridge_factory,
        )

    assert result["action"] == "wait"
    from store.db import get_trades
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
            llm_fn=decide,
            bridge_factory=bridge_factory,
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
            llm_fn=decide,
            bridge_factory=bridge_factory,
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
