"""Tests for store/query.py."""
from store.db import insert_agent, insert_trade
from store.fingerprint import write_entry, write_outcome
from store.query import (
    query_trades, count_trades, get_trade, win_rate, summarize,
    format_trades_summary,
)

AGENTS = ["jade_hawk", "iron_moth"]


def _seed_trade(conn, trade_id, agent_id, asset, direction, regime,
                 funding, oi_chg, result, pnl_pct, status="closed"):
    insert_trade(conn, {
        "id": trade_id,
        "agent_id": agent_id,
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": asset,
        "direction": direction,
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": f"2026-06-{trade_id[-2:]}T12:00:00Z",
        "status": status,
    })
    write_entry(conn, trade_id, {
        "ohlcv_15m": [[1, 1.0, 2.0, 0.5, 1.5, 100.0]],
        "funding_rate_current": funding,
        "open_interest_24h_change_pct": oi_chg,
    }, regime=regime)
    if status == "closed":
        write_outcome(conn, trade_id, {
            "exit_price": 100.0 * (1 + pnl_pct),
            "pnl_pct": pnl_pct,
            "result": result,
        })


def _seed_desk(conn):
    for a in AGENTS:
        insert_agent(conn, a, a, "2026-06-29T00:00:00Z", "{}")

    # SOL longs with negative funding: 3 wins, 1 loss -> 75% win rate
    _seed_trade(conn, "t01", "jade_hawk", "SOL-PERP", "long", "range_high_vol", -0.004, -3.2, "win", 0.03)
    _seed_trade(conn, "t02", "jade_hawk", "SOL-PERP", "long", "range_high_vol", -0.006, -4.0, "win", 0.02)
    _seed_trade(conn, "t03", "iron_moth", "SOL-PERP", "long", "trending_bull", -0.002, -1.0, "win", 0.015)
    _seed_trade(conn, "t04", "iron_moth", "SOL-PERP", "long", "range_high_vol", -0.001, -0.5, "loss", -0.01)
    # SOL longs with positive funding: should not count toward the negative-funding query
    _seed_trade(conn, "t05", "jade_hawk", "SOL-PERP", "long", "range_high_vol", 0.002, 1.0, "loss", -0.02)
    # ETH shorts: irrelevant noise
    _seed_trade(conn, "t06", "iron_moth", "ETH-PERP", "short", "trending_bear", 0.001, 2.0, "win", 0.01)
    # Open SOL long (no outcome yet)
    _seed_trade(conn, "t07", "jade_hawk", "SOL-PERP", "long", "range_high_vol", -0.003, -2.0, None, None, status="open")


def test_query_trades_sol_longs_negative_funding_win_rate(conn):
    _seed_desk(conn)
    results = query_trades(
        conn, asset="SOL-PERP", direction="long", status="closed",
        funding_rate_max=0, decode_ohlcv=False,
    )
    assert len(results) == 4  # t01, t02, t03, t04
    assert win_rate(results) == 0.75


def test_query_trades_cross_agent_default(conn):
    _seed_desk(conn)
    all_trades = query_trades(conn, decode_ohlcv=False, limit=100)
    agent_ids = {t["agent_id"] for t in all_trades}
    assert agent_ids == set(AGENTS)


def test_query_trades_scoped_to_single_agent(conn):
    _seed_desk(conn)
    own = query_trades(conn, agent_id="jade_hawk", decode_ohlcv=False, limit=100)
    assert all(t["agent_id"] == "jade_hawk" for t in own)
    assert len(own) == 4  # t01, t02, t05, t07


def test_query_trades_regime_filter(conn):
    _seed_desk(conn)
    results = query_trades(conn, regime="trending_bull", decode_ohlcv=False)
    assert len(results) == 1
    assert results[0]["id"] == "t03"


def test_query_trades_decodes_ohlcv_by_default(conn):
    _seed_desk(conn)
    results = query_trades(conn, asset="SOL-PERP", limit=1)
    assert "ohlcv_15m" in results[0]
    assert results[0]["ohlcv_15m"] == [[1, 1.0, 2.0, 0.5, 1.5, 100.0]]
    assert "ohlcv_15m_40_blob" not in results[0]


def test_count_trades_matches_query_filters(conn):
    _seed_desk(conn)
    n = count_trades(conn, asset="SOL-PERP", direction="long", status="closed", funding_rate_max=0)
    assert n == 4


def test_get_trade_single_lookup(conn):
    _seed_desk(conn)
    t = get_trade(conn, "t01")
    assert t["asset"] == "SOL-PERP"
    assert t["regime"] == "range_high_vol"
    t_missing = get_trade(conn, "does_not_exist")
    assert t_missing is None


def test_summarize_helper(conn):
    _seed_desk(conn)
    # SOL longs, negative funding only (excludes t05, which has positive funding)
    results = query_trades(
        conn, asset="SOL-PERP", direction="long", status="closed",
        funding_rate_max=0, decode_ohlcv=False,
    )
    s = summarize(results)
    assert s["closed_count"] == 4
    assert s["wins"] == 3
    assert s["losses"] == 1
    assert s["win_rate"] == 0.75


def test_format_trades_summary_handles_closed_trade_with_no_result(conn):
    # A trade can be closed (e.g. by a manual/stop-loss exit) before a
    # win/loss classification is written, leaving result=None. This must
    # not crash format_trades_summary's status-fallback formatting.
    _seed_desk(conn)
    _seed_trade(conn, "t08", "jade_hawk", "SOL-PERP", "long",
                "range_high_vol", -0.002, -1.5, None, 0.0)
    trades = query_trades(conn, agent_id="jade_hawk", decode_ohlcv=False)
    text = format_trades_summary(trades)
    assert "SOL-PERP" in text
    assert "closed" in text  # falls back to status when result is None
