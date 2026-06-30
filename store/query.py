"""
store/query.py — structured query builder over the trade bank.

query_trades() is the single entry point for both in-process callers
(agent decision prompts, reflection, head-of-desk) and the /api/query
web endpoint. agent_id=None (the default) queries across every agent —
this is what makes it a *cross-agent* query, per the M4 spec.
"""
import json

from store.fingerprint import unpack_ohlcv

# Columns that hold JSON-encoded lists/dicts and should be decoded on read.
_JSON_COLUMNS = ("key_conditions_met", "key_conditions_missing",
                  "funding_rate_8h_history", "market_context_json", "agent_reasoning_json")


def query_trades(
    conn,
    agent_id: str | None = None,
    asset: str | None = None,
    direction: str | None = None,
    regime: str | None = None,
    outcome: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    funding_rate_min: float | None = None,
    funding_rate_max: float | None = None,
    oi_change_min: float | None = None,
    oi_change_max: float | None = None,
    order_by: str = "entry_timestamp DESC",
    limit: int = 200,
    offset: int = 0,
    decode_ohlcv: bool = True,
) -> list[dict]:
    """Query the trade bank with arbitrary filters.

    agent_id=None (default) searches across all agents — this is the
    cross-agent query used by reflection / head-of-desk / the /trades page.
    Pass an agent_id to scope to a single trader's own history.

    `outcome` filters on the `result` column ('win' / 'loss'). `status`
    filters on trade status ('open' / 'closed') independently of outcome.
    funding_rate_* and oi_change_* are inclusive range filters against the
    funding_rate_current / open_interest_24h_change_pct columns captured
    at entry.

    Returns a list of trade dicts with OHLCV blobs decoded back into
    candle arrays (unless decode_ohlcv=False, e.g. for lightweight list
    views) and JSON columns parsed back into Python objects.
    """
    clauses = []
    params: list = []

    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if asset is not None:
        clauses.append("asset = ?")
        params.append(asset)
    if direction is not None:
        clauses.append("direction = ?")
        params.append(direction)
    if regime is not None:
        clauses.append("regime = ?")
        params.append(regime)
    if outcome is not None:
        clauses.append("result = ?")
        params.append(outcome)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if date_from is not None:
        clauses.append("entry_timestamp >= ?")
        params.append(date_from)
    if date_to is not None:
        clauses.append("entry_timestamp <= ?")
        params.append(date_to)
    if funding_rate_min is not None:
        clauses.append("funding_rate_current >= ?")
        params.append(funding_rate_min)
    if funding_rate_max is not None:
        clauses.append("funding_rate_current <= ?")
        params.append(funding_rate_max)
    if oi_change_min is not None:
        clauses.append("open_interest_24h_change_pct >= ?")
        params.append(oi_change_min)
    if oi_change_max is not None:
        clauses.append("open_interest_24h_change_pct <= ?")
        params.append(oi_change_max)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM trades {where} ORDER BY {order_by} LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(sql, params).fetchall()
    return [_decode_row(dict(r), decode_ohlcv) for r in rows]


def _decode_row(row: dict, decode_ohlcv: bool) -> dict:
    for col in _JSON_COLUMNS:
        val = row.get(col)
        if val:
            try:
                row[col] = json.loads(val)
            except (TypeError, ValueError):
                pass

    if decode_ohlcv:
        for blob_col, out_key in (
            ("ohlcv_15m_blob", "ohlcv_15m"),
            ("ohlcv_1h_blob", "ohlcv_1h"),
            ("ohlcv_4h_blob", "ohlcv_4h"),
        ):
            row[out_key] = unpack_ohlcv(row.get(blob_col))
            row.pop(blob_col, None)
    else:
        for blob_col in ("ohlcv_15m_blob", "ohlcv_1h_blob", "ohlcv_4h_blob"):
            row.pop(blob_col, None)

    return row


def win_rate(trades: list[dict]) -> float:
    """Win rate across a list of trade dicts (closed trades only)."""
    closed = [t for t in trades if t.get("result") in ("win", "loss")]
    if not closed:
        return 0.0
    wins = sum(1 for t in closed if t.get("result") == "win")
    return wins / len(closed)


def summarize(trades: list[dict]) -> dict:
    """Small aggregate summary used by prompt sections and the API."""
    closed = [t for t in trades if t.get("result") in ("win", "loss")]
    wins = [t for t in closed if t.get("result") == "win"]
    avg_pnl = (
        sum(t.get("pnl_pct", 0) or 0 for t in closed) / len(closed)
        if closed else 0.0
    )
    return {
        "count": len(trades),
        "closed_count": len(closed),
        "win_rate": win_rate(trades),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "avg_pnl_pct": avg_pnl,
    }
