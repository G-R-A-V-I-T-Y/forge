"""Desk-wide position registry — queries across all agents.

Reads from the existing `positions` table created by store/db.py.
This is the desk-wide view, not per-agent position CRUD.
"""
from datetime import datetime, timezone


def get_all_open_positions(conn) -> list[dict]:
    """Return all open positions across EVERY agent (desk-wide view)."""
    rows = conn.execute(
        "SELECT * FROM positions ORDER BY agent_id, opened_at"
    ).fetchall()
    return [dict(r) for r in rows]


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

    now = datetime.now(timezone.utc)
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
                delta = now - opened_dt
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
