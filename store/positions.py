"""Desk-wide position registry — queries across all agents.

Reads from the existing `positions` table created by store/db.py.
This is the desk-wide view, not per-agent position CRUD.
"""
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TRADE_LEDGER_EXCLUDE_COLUMNS = {
    # Redundant with the candles_5m/funding ledger (Task 4) once a trade's
    # timestamp is known, and raw `bytes` blobs don't round-trip through
    # JSON -- exporting them would either crash json.dumps or silently
    # write an unrestorable str(bytes) repr.
    "ohlcv_15m_40_blob", "ohlcv_1h_20_blob", "ohlcv_4h_10_blob", "funding_history_blob",
}


def get_all_open_positions(conn) -> list[dict]:
    """Return all open positions across EVERY agent (desk-wide view)."""
    rows = conn.execute(
        "SELECT * FROM positions ORDER BY agent_id, opened_at"
    ).fetchall()
    return [dict(r) for r in rows]


def has_open_position_for_asset(conn, agent_id: str, asset: str) -> bool:
    """Check if an agent has any open position for a given asset."""
    row = conn.execute(
        "SELECT 1 FROM trades WHERE agent_id = ? AND asset = ? AND status = 'open' LIMIT 1",
        (agent_id, asset),
    ).fetchone()
    return row is not None


def find_first_cross(candles_5m, entry_ts_unix, direction, sl, tp):
    """Scan 5m candles from entry timestamp forward to find first SL/TP cross.

    candles_5m is a list of [ts_ms, open, high, low, close, volume].
    entry_ts_unix is the entry timestamp in seconds.
    Returns (price, reason) or None.

    This is a pure function with no I/O.
    """
    for c in candles_5m:
        if c[0] < entry_ts_unix * 1000:
            continue
        lo = c[3]
        hi = c[2]
        if direction == "long":
            if sl is not None and lo <= sl:
                return (sl, "stop_loss")
            if tp is not None and hi >= tp:
                return (tp, "take_profit")
        else:
            if sl is not None and hi >= sl:
                return (sl, "stop_loss")
            if tp is not None and lo <= tp:
                return (tp, "take_profit")
    return None


def _calculate_funding(position, close_ts_unix, funding_history):
    """Compute net funding PnL between entry and close.

    funding_history is a list of dicts with {"time": ms_timestamp, "fundingRate": float}.
    position dict has notional_usd, entry_price, direction, opened_at.
    Returns the total funding PnL (positive = PnL gain, negative = PnL cost).
    """
    if not funding_history:
        return 0.0

    notional = position.get("notional_usd", 0.0)
    entry_price = position.get("entry_price", 0.0)
    direction = position.get("direction", "long")
    opened_at = position.get("opened_at")

    if notional <= 0 or entry_price <= 0 or not opened_at:
        return 0.0

    position_size = notional / entry_price
    entry_ts = _parse_entry_ts(opened_at)
    if entry_ts is None:
        return 0.0

    total_payment = 0.0
    samples = 0
    for ev in funding_history:
        ev_ts_ms = ev.get("time", 0)
        rate = ev.get("fundingRate", 0.0)
        if rate is None:
            continue
        if entry_ts * 1000 <= ev_ts_ms <= close_ts_unix * 1000:
            total_payment += position_size * rate
            samples += 1

    duration_hours = (close_ts_unix - entry_ts) / 3600

    if duration_hours > 72 and samples < duration_hours * 0.5:
        all_rates = [
            ev.get("fundingRate", 0.0)
            for ev in funding_history
            if ev.get("fundingRate") is not None
        ]
        if all_rates:
            avg_rate = sum(all_rates) / len(all_rates)
            total_payment = position_size * avg_rate * duration_hours

    if direction == "long":
        return -total_payment
    else:
        return total_payment


def _parse_entry_ts(opened_at_str):
    """Parse ISO timestamp string to Unix seconds. Returns None on failure."""
    if not opened_at_str:
        return None
    try:
        dt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def execute_close(
    conn, position_id, exit_price, reason, config, position_dict, funding_history
):
    """Execute a position close: compute net PnL, update trades/accounts.

    config dict has taker_fee.
    position_dict is the full position row as a dict.
    funding_history is the heartbeat's asset funding history list.

    Returns dict with trade_id, exit_price, pnl_pct, pnl_usd, fees_paid, funding_paid.
    """
    taker_fee = config.get("taker_fee", 0.00035)

    direction = position_dict["direction"]
    entry = position_dict["entry_price"]
    leverage = position_dict.get("leverage", 1)
    notional = position_dict["notional_usd"]

    if direction == "long":
        pnl_pct = (exit_price - entry) / entry * leverage
    else:
        pnl_pct = (entry - exit_price) / entry * leverage

    gross_pnl_usd = notional * pnl_pct
    entry_fee = notional * taker_fee
    exit_fee = notional * taker_fee

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    close_ts = time.time()

    # Calculate duration in minutes
    entry_ts = _parse_entry_ts(position_dict.get("opened_at"))
    duration_minutes = 0
    if entry_ts is not None:
        duration_minutes = max(0, int((close_ts - entry_ts) / 60))

    funding_pnl = _calculate_funding(position_dict, close_ts, funding_history)

    net_pnl_usd = gross_pnl_usd - entry_fee - exit_fee + funding_pnl
    net_pnl_pct = net_pnl_usd / notional

    account = conn.execute(
        "SELECT balance, peak_balance FROM accounts WHERE agent_id = ? AND mode = ? ORDER BY id DESC LIMIT 1",
        (position_dict["agent_id"], position_dict.get("mode", "paper")),
    ).fetchone()

    if account:
        old_balance = account["balance"]
        old_peak = account["peak_balance"]
    else:
        old_balance = notional / (position_dict.get("position_size_pct", 0.1) or 0.1)
        old_peak = old_balance

    new_balance = old_balance + net_pnl_usd
    peak = max(old_peak, new_balance)

    with conn:
        conn.execute(
            """UPDATE trades SET status='closed', exit_price=?, exit_timestamp=?,
               exit_reason=?, pnl_pct=?, pnl_usd=?, result=?,
               fees_paid=?, funding_paid=?, duration_minutes=? WHERE id=?""",
            (
                exit_price,
                now,
                reason,
                net_pnl_pct,
                net_pnl_usd,
                "win" if net_pnl_usd > 0 else "loss",
                entry_fee + exit_fee,
                -funding_pnl,
                duration_minutes,
                position_dict["trade_id"],
            ),
        )
        conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        conn.execute(
            "INSERT INTO accounts (agent_id, mode, balance, peak_balance, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (
                position_dict["agent_id"],
                position_dict.get("mode", "paper"),
                new_balance,
                peak,
                now,
            ),
        )

    from store.ledger import append_ledger_record

    full_trade = conn.execute(
        "SELECT * FROM trades WHERE id = ?", (position_dict["trade_id"],)
    ).fetchone()
    if full_trade:
        record = {
            k: v for k, v in dict(full_trade).items()
            if k not in _TRADE_LEDGER_EXCLUDE_COLUMNS
        }
        append_ledger_record("trades", record)

    append_ledger_record(
        "accounts",
        {
            "ts": now,
            "agent_id": position_dict["agent_id"],
            "mode": position_dict.get("mode", "paper"),
            "balance": new_balance,
            "peak_balance": peak,
        },
    )

    return {
        "trade_id": position_dict["trade_id"],
        "exit_price": exit_price,
        "pnl_pct": net_pnl_pct,
        "pnl_usd": net_pnl_usd,
        "fees_paid": entry_fee + exit_fee,
        "funding_paid": -funding_pnl,
        "duration_minutes": duration_minutes,
    }


def get_desk_positions_summary(conn, exclude_agent_id: str | None = None) -> str:
    """Return a formatted string for LLM prompt context.

    Shows agent, asset, direction, entry, current P&L, and duration for
    every open position across the desk, optionally excluding one agent
    (so that agent sees the *other* traders' positions).
    """
    if exclude_agent_id:
        rows = conn.execute(
            "SELECT * FROM positions WHERE agent_id != ? ORDER BY agent_id, opened_at",
            (exclude_agent_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY agent_id, opened_at"
        ).fetchall()

    if not rows:
        return "  No open positions on the desk."

    now_dt = datetime.now(timezone.utc)
    lines = []
    for row in rows:
        pos = dict(row)
        agent = pos["agent_id"]
        asset = pos["asset"]
        direction = pos["direction"].upper()
        entry = pos["entry_price"]
        opened = pos["opened_at"]

        duration_str = ""
        if opened:
            try:
                opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                delta = now_dt - opened_dt
                if delta.days > 0:
                    duration_str = f"{delta.days}d {delta.seconds // 3600}h ago"
                elif delta.seconds // 3600 > 0:
                    duration_str = (
                        f"{delta.seconds // 3600}h {(delta.seconds // 60) % 60}m ago"
                    )
                else:
                    duration_str = f"{delta.seconds // 60}m ago"
            except (ValueError, TypeError):
                pass

        current_pnl = pos.get("current_pnl_pct") or 0.0
        pnl_str = f"{current_pnl:+.1%}" if current_pnl else "0.0%"

        if duration_str:
            line = (
                f"  {agent:14s} {direction:6s} {asset:10s} @ ${entry:,.2f}  ({pnl_str})"
                f"  — entry {duration_str}"
            )
        else:
            line = (
                f"  {agent:14s} {direction:6s} {asset:10s} @ ${entry:,.2f}  ({pnl_str})"
            )
        lines.append(line)

    return "\n".join(lines)


def update_position_pnl(conn, assets_data: dict) -> None:
    rows = conn.execute(
        "SELECT * FROM positions ORDER BY agent_id, opened_at"
    ).fetchall()
    for row in rows:
        pos = dict(row)
        asset = pos["asset"]
        direction = pos["direction"]
        entry = pos["entry_price"]
        position_id = pos["id"]
        leverage = pos.get("leverage", 1)

        asset_data = assets_data.get(asset)
        if asset_data is None:
            continue
        current_price = asset_data.get("price")
        if current_price is None or entry is None or entry == 0:
            continue

        if direction == "long":
            pnl = (current_price - entry) / entry * leverage
        else:
            pnl = (entry - current_price) / entry * leverage

        conn.execute(
            "UPDATE positions SET current_pnl_pct = ? WHERE id = ?",
            (pnl, position_id),
        )
    conn.commit()


async def reconcile_positions(conn, assets_data: dict, provider, config: dict) -> int:
    """Check all open positions against SL/TP using candle reconstruction.

    For positions whose current price is outside SL/TP bounds, scans 5m
    candle data from the heartbeat to find the first cross point. If the
    heartbeat's 25h candle window doesn't cover the gap, fetches additional
    candles from the provider.

    Returns the number of positions closed.
    """

    taker_fee = config.get("desk", {}).get("taker_fee", 0.00035)

    rows = conn.execute("SELECT * FROM positions").fetchall()
    closed_count = 0

    for row in rows:
        pos = dict(row)
        asset = pos["asset"]
        asset_data = assets_data.get(asset)
        if not asset_data:
            continue

        current_price = asset_data.get("price")
        if current_price is None or current_price <= 0:
            continue

        sl = pos["stop_loss_price"]
        tp = pos["take_profit_price"]
        direction = pos["direction"]

        entry_ts = _parse_entry_ts(pos.get("opened_at"))
        now_ts = time.time()

        sltp_within = False
        if direction == "long":
            if sl is not None and tp is not None and sl < current_price < tp:
                sltp_within = True
            elif sl is None and tp is not None and current_price < tp:
                sltp_within = True
            elif tp is None and sl is not None and current_price > sl:
                sltp_within = True
        else:
            if sl is not None and tp is not None and tp < current_price < sl:
                sltp_within = True
            elif sl is None and tp is not None and current_price > tp:
                sltp_within = True
            elif tp is None and sl is not None and current_price < sl:
                sltp_within = True

        if sltp_within:
            max_hold_hours = pos.get("max_hold_hours")
            if max_hold_hours is not None and entry_ts is not None:
                elapsed_hours = (now_ts - entry_ts) / 3600
                if elapsed_hours >= max_hold_hours:
                    logger.info("max_hold reached for %s at %.2f (held %.1fh)", asset, current_price, elapsed_hours)
                    funding_history = asset_data.get("funding_history", []) or []
                    result = execute_close(
                        conn=conn,
                        position_id=pos["id"],
                        exit_price=current_price,
                        reason="max_hold",
                        config={"taker_fee": taker_fee},
                        position_dict=pos,
                        funding_history=funding_history,
                    )
                    if result:
                        logger.info(
                            "Max hold closed %s at %.2f (net pnl=%.2f%%, fees=%.4f)",
                            result["trade_id"],
                            current_price,
                            (result.get("pnl_pct") or 0) * 100,
                            result.get("fees_paid", 0),
                        )
                        closed_count += 1
            continue

        candles = list(asset_data.get("candles_5m", []) or [])
        cross = find_first_cross(candles, entry_ts, direction, sl, tp)

        if cross is None and provider is not None and entry_ts is not None:
            gap_hours = (now_ts - entry_ts) / 3600 if entry_ts else 0
            if gap_hours > 25:
                try:
                    extra_candles = await _fetch_extra_candles(
                        provider, asset, entry_ts, now_ts
                    )
                    seen_ts = {c[0] for c in candles}
                    for c in extra_candles:
                        if c[0] not in seen_ts:
                            candles.append(c)
                            seen_ts.add(c[0])
                    candles.sort(key=lambda c: c[0])
                    cross = find_first_cross(candles, entry_ts, direction, sl, tp)
                except Exception as e:
                    logger.warning("Failed to fetch extra candles for %s: %s", asset, e)

        if cross:
            exit_price, reason = cross
        else:
            if direction == "long":
                reason = "stop_loss" if current_price <= sl else "take_profit"
            else:
                reason = "stop_loss" if current_price >= sl else "take_profit"
            exit_price = current_price

        funding_history = asset_data.get("funding_history", []) or []

        result = execute_close(
            conn=conn,
            position_id=pos["id"],
            exit_price=exit_price,
            reason=reason,
            config={"taker_fee": taker_fee},
            position_dict=pos,
            funding_history=funding_history,
        )
        if result:
            logger.info(
                "%s closed %s at %.2f (net pnl=%.2f%%, fees=%.4f, funding=%.4f)",
                reason.replace("_", " ").title(),
                result["trade_id"],
                exit_price,
                (result.get("pnl_pct") or 0) * 100,
                result.get("fees_paid", 0),
                result.get("funding_paid", 0),
            )
            closed_count += 1

    return closed_count


async def _fetch_extra_candles(provider, asset: str, since_ts: float, until_ts: float):
    """Fetch 5m candles from provider to fill gaps beyond heartbeat window.

    since_ts and until_ts are Unix timestamps in seconds.
    """
    gap_hours = (until_ts - since_ts) / 3600
    lookback_candles = max(1, int(gap_hours / (5 / 60)) + 10)
    try:
        candles = await provider.get_ohlcv(asset, "5m", lookback_candles)
        return candles or []
    except Exception:
        return []
