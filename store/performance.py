"""Rolling performance metrics from the trades table."""
import statistics
from datetime import datetime, timezone


def compute_metrics(conn, agent_id: str) -> dict:
    """Compute all performance metrics for an agent from SQLite.

    Returns a dict with keys: win_rate, profit_factor, avg_win_pct,
    avg_loss_pct, avg_wl_ratio, sharpe, total_trades, closed_trades,
    best_trade_pct, worst_trade_pct, by_regime, last_20_win_rate,
    last_20_pf, last_7d_return.
    """
    rows = conn.execute(
        """SELECT * FROM trades WHERE agent_id = ? AND status = 'closed'
           ORDER BY entry_timestamp DESC""",
        (agent_id,),
    ).fetchall()
    closed = [dict(r) for r in rows]

    total = len(closed)
    if total == 0:
        return {
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "avg_wl_ratio": 0.0,
            "sharpe": 0.0,
            "total_trades": 0,
            "closed_trades": 0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
            "by_regime": {},
            "last_20_win_rate": 0.0,
            "last_20_pf": 0.0,
            "last_7d_return": 0.0,
        }

    wins = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]

    win_rate = len(wins) / total if total else 0.0
    total_wins = sum(t.get("pnl_pct", 0) or 0 for t in wins)
    total_losses = abs(sum(t.get("pnl_pct", 0) or 0 for t in losses))
    profit_factor = total_wins / total_losses if total_losses else float("inf")

    avg_win_pct = statistics.mean([t["pnl_pct"] for t in wins]) if wins else 0.0
    avg_loss_pct = statistics.mean([t["pnl_pct"] for t in losses]) if losses else 0.0
    avg_wl_ratio = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct else 0.0

    all_pnl = [t.get("pnl_pct", 0) or 0 for t in closed]
    sharpe = _compute_sharpe(all_pnl)

    best_trade_pct = max(t.get("pnl_pct", 0) or 0 for t in closed)
    worst_trade_pct = min(t.get("pnl_pct", 0) or 0 for t in closed)

    by_regime = _by_regime(closed)

    last_20 = closed[:20]
    last_20_wins = [t for t in last_20 if t.get("result") == "win"]
    last_20_win_rate = len(last_20_wins) / len(last_20) if last_20 else 0.0
    last_20_win_total = sum(t.get("pnl_pct", 0) or 0 for t in last_20_wins)
    last_20_loss_total = abs(sum(t.get("pnl_pct", 0) or 0 for t in last_20 if t.get("result") == "loss"))
    last_20_pf = last_20_win_total / last_20_loss_total if last_20_loss_total else float("inf")

    now = datetime.now(timezone.utc)
    last_7d = [t for t in closed if _parse_ts(t.get("entry_timestamp")) and (now - _parse_ts(t["entry_timestamp"])).days < 7]
    last_7d_return = sum(t.get("pnl_pct", 0) or 0 for t in last_7d)

    return {
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "avg_win_pct": round(avg_win_pct, 4),
        "avg_loss_pct": round(avg_loss_pct, 4),
        "avg_wl_ratio": round(avg_wl_ratio, 4),
        "sharpe": round(sharpe, 4),
        "total_trades": len(closed) + conn.execute(
            "SELECT COUNT(*) FROM trades WHERE agent_id = ? AND status = 'open'",
            (agent_id,),
        ).fetchone()[0],
        "closed_trades": total,
        "best_trade_pct": round(best_trade_pct, 4),
        "worst_trade_pct": round(worst_trade_pct, 4),
        "by_regime": {k: round(v, 4) for k, v in by_regime.items()},
        "last_20_win_rate": round(last_20_win_rate, 4),
        "last_20_pf": round(last_20_pf, 4) if last_20_pf != float("inf") else 0.0,
        "last_7d_return": round(last_7d_return, 4),
    }


def _compute_sharpe(pnl_list: list[float]) -> float:
    if len(pnl_list) < 2:
        return 0.0
    avg = statistics.mean(pnl_list)
    std = statistics.stdev(pnl_list)
    return avg / std if std > 0 else 0.0


def _by_regime(trades: list[dict]) -> dict:
    regime_map = {}
    for t in trades:
        # Regime may be stored in market_context or a separate field
        # For now, skip regime breakdown if not available
        pass
    return regime_map


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def format_performance_summary(metrics: dict, agent_name: str) -> str:
    """Format metrics into a human-readable performance block for the prompt."""
    lines = [
        f"PERFORMANCE SUMMARY — {agent_name}",
        "─" * 55,
    ]
    lines.append(f"  Closed trades: {metrics['closed_trades']}")
    lines.append(f"  Win rate:      {metrics['win_rate']:.1%}")
    lines.append(f"  Profit factor: {metrics['profit_factor']:.2f}")
    lines.append(f"  Avg win:      +{metrics['avg_win_pct']:.1%}")
    lines.append(f"  Avg loss:      {metrics['avg_loss_pct']:.1%}")
    lines.append(f"  Avg W/L ratio: {metrics['avg_wl_ratio']:.2f}")
    lines.append(f"  Sharpe:        {metrics['sharpe']:.2f}")
    lines.append(f"  Best trade:   +{metrics['best_trade_pct']:.1%}")
    lines.append(f"  Worst trade:   {metrics['worst_trade_pct']:.1%}")
    lines.append("")
    if metrics["closed_trades"] >= 20:
        lines.append(f"  LAST 20:")
        lines.append(f"    Win rate: {metrics['last_20_win_rate']:.1%}")
        lines.append(f"    PF:       {metrics['last_20_pf']:.2f}")
    if metrics["last_7d_return"] != 0.0:
        lines.append(f"  LAST 7 DAYS: {metrics['last_7d_return']:+.2%}")
    if metrics["by_regime"]:
        lines.append("")
        lines.append("  BY REGIME:")
        for regime, wr in sorted(metrics["by_regime"].items()):
            lines.append(f"    {regime}: {wr:.1%} WR")
    return "\n".join(lines)
