"""Rolling performance metrics from the trades table.

M6 rewrite: equity-curve Sharpe/Sortino, capped profit_factor,
exposure-adjusted returns, leaderboard vs null benchmark.
"""
import statistics
from datetime import datetime, timezone


def compute_metrics(conn, agent_id: str) -> dict:
    """Compute all performance metrics for an agent from SQLite.

    Excludes voided trades.  Returns a dict with keys: win_rate,
    profit_factor (capped at 10), avg_win_pct, avg_loss_pct,
    avg_wl_ratio, sharpe (equity-curve), sortino, total_trades,
    closed_trades, best_trade_pct, worst_trade_pct, exposure_adjusted_return,
    by_regime, last_20_win_rate, last_20_pf, last_7d_return,
    benchmark_vs_null.
    """
    rows = conn.execute(
        """SELECT * FROM trades WHERE agent_id = ? AND status = 'closed'
           AND voided = 0
           ORDER BY entry_timestamp ASC""",
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
            "sortino": 0.0,
            "total_trades": 0,
            "closed_trades": 0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
            "exposure_adjusted_return": 0.0,
            "by_regime": {},
            "last_20_win_rate": 0.0,
            "last_20_pf": 0.0,
            "last_7d_return": 0.0,
            "benchmark_vs_null": 0.0,
        }

    wins = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]

    win_rate = len(wins) / total if total else 0.0
    total_wins = sum(t.get("pnl_pct", 0) or 0 for t in wins)
    total_losses = abs(sum(t.get("pnl_pct", 0) or 0 for t in losses))
    # Cap profit_factor at 10 to prevent inf values from skewing leaderboards
    profit_factor = min(total_wins / total_losses if total_losses else 10.0, 10.0)

    avg_win_pct = statistics.mean([t["pnl_pct"] for t in wins]) if wins else 0.0
    avg_loss_pct = statistics.mean([t["pnl_pct"] for t in losses]) if losses else 0.0
    avg_wl_ratio = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct else 0.0

    # Equity-curve Sharpe (trades ordered chronologically)
    all_pnl = [t.get("pnl_pct", 0) or 0 for t in closed]
    sharpe = _compute_sharpe(all_pnl)
    sortino = _compute_sortino(all_pnl)

    # Exposure-adjusted return: total pnl / mean notional exposure
    notional_exposures = [
        (t.get("position_size_pct", 0) or 0) * (t.get("leverage", 1) or 1)
        for t in closed
    ]
    mean_exposure = statistics.mean(notional_exposures) if notional_exposures else 1.0
    exposure_adjusted_return = (
        sum(all_pnl) / mean_exposure if mean_exposure > 0 else 0.0
    )

    best_trade_pct = max(t.get("pnl_pct", 0) or 0 for t in closed)
    worst_trade_pct = min(t.get("pnl_pct", 0) or 0 for t in closed)

    by_regime = _by_regime(closed)

    last_20 = closed[:20]
    last_20_wins = [t for t in last_20 if t.get("result") == "win"]
    last_20_win_rate = len(last_20_wins) / len(last_20) if last_20 else 0.0
    last_20_win_total = sum(t.get("pnl_pct", 0) or 0 for t in last_20_wins)
    last_20_loss_total = abs(
        sum(t.get("pnl_pct", 0) or 0 for t in last_20 if t.get("result") == "loss")
    )
    last_20_pf = min(
        last_20_win_total / last_20_loss_total if last_20_loss_total else 10.0, 10.0
    )

    now = datetime.now(timezone.utc)
    last_7d = [
        t
        for t in closed
        if _parse_ts(t.get("entry_timestamp"))
        and (now - _parse_ts(t["entry_timestamp"])).days < 7
    ]
    last_7d_return = sum(t.get("pnl_pct", 0) or 0 for t in last_7d)

    # Benchmark vs null: how much better is this agent than a null strategy
    # (which would have 0 return).  Positive = outperforming null, negative = underperforming.
    benchmark_vs_null = sum(all_pnl)

    return {
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "avg_win_pct": round(avg_win_pct, 4),
        "avg_loss_pct": round(avg_loss_pct, 4),
        "avg_wl_ratio": round(avg_wl_ratio, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "total_trades": len(closed)
        + conn.execute(
            "SELECT COUNT(*) FROM trades WHERE agent_id = ? AND status = 'open' AND voided = 0",
            (agent_id,),
        ).fetchone()[0],
        "closed_trades": total,
        "best_trade_pct": round(best_trade_pct, 4),
        "worst_trade_pct": round(worst_trade_pct, 4),
        "exposure_adjusted_return": round(exposure_adjusted_return, 4),
        "by_regime": {k: round(v, 4) for k, v in by_regime.items()},
        "last_20_win_rate": round(last_20_win_rate, 4),
        "last_20_pf": round(last_20_pf, 4),
        "last_7d_return": round(last_7d_return, 4),
        "benchmark_vs_null": round(benchmark_vs_null, 4),
    }


def _compute_sharpe(pnl_list: list[float]) -> float:
    """Sharpe ratio of an equity curve (list of period returns)."""
    if len(pnl_list) < 2:
        return 0.0
    avg = statistics.mean(pnl_list)
    std = statistics.stdev(pnl_list)
    return avg / std if std > 0 else 0.0


def _compute_sortino(pnl_list: list[float]) -> float:
    """Sortino ratio: downside deviation instead of total stdev."""
    if len(pnl_list) < 2:
        return 0.0
    avg = statistics.mean(pnl_list)
    negative_returns = [r for r in pnl_list if r < 0]
    if not negative_returns:
        return avg  # No downside — ratio is just the mean return
    downside_std = (
        statistics.stdev(negative_returns)
        if len(negative_returns) > 1
        else abs(statistics.mean(negative_returns))
    )
    return avg / downside_std if downside_std > 0 else 0.0


def _by_regime(trades: list[dict]) -> dict:
    """Compute win rate by market regime."""
    regime_data: dict[str, list[bool]] = {}
    for t in trades:
        regime = t.get("regime") or "unknown"
        if regime not in regime_data:
            regime_data[regime] = []
        regime_data[regime].append(t.get("result") == "win")

    result = {}
    for regime, wins in regime_data.items():
        result[regime] = sum(wins) / len(wins) if wins else 0.0
    return result


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def compute_calibration_curve(
    conn, agent_id: str,
) -> dict[str, dict]:
    """Compute confidence vs. realized win-rate calibration buckets.

    Groups closed trades by confidence decile (0.0–0.1, 0.1–0.2, …,
    0.9–1.0) and returns the observed win rate for each bucket.  A
    well-calibrated agent has confidence ≈ win rate in every bucket.

    Returns a ``dict[str, dict]`` keyed by bucket label (e.g.
    ``"0.7-0.8"``) with each value containing:

    - ``confidence_mid``: midpoint of the bucket (float)
    - ``realized_wr``: realized win rate in this bucket (float)
    - ``sample_count``: number of trades in this bucket (int)
    """
    rows = conn.execute(
        """SELECT confidence, result FROM trades
           WHERE agent_id = ? AND status = 'closed' AND voided = 0
           AND confidence IS NOT NULL""",
        (agent_id,),
    ).fetchall()

    if not rows:
        return {}

    buckets: dict[str, dict[str, float]] = {}
    for r in rows:
        conf = r["confidence"]
        # Bucket into deciles: 0.0-0.1, 0.1-0.2, ...
        bucket_idx = min(int(conf * 10), 9)
        bucket_label = f"{bucket_idx / 10:.1f}-{(bucket_idx + 1) / 10:.1f}"
        if bucket_label not in buckets:
            buckets[bucket_label] = {"wins": 0, "total": 0}
        buckets[bucket_label]["total"] += 1
        if r["result"] == "win":
            buckets[bucket_label]["wins"] += 1

    result: dict[str, dict] = {}
    for bucket_label in sorted(buckets.keys()):
        data = buckets[bucket_label]
        total = data["total"]
        result[bucket_label] = {
            "confidence_mid": (float(bucket_label.split("-")[0]) + float(bucket_label.split("-")[1])) / 2,
            "realized_wr": data["wins"] / total if total > 0 else 0.0,
            "sample_count": total,
        }
    return result


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
    lines.append(f"  Sortino:       {metrics['sortino']:.2f}")
    lines.append(f"  Best trade:   +{metrics['best_trade_pct']:.1%}")
    lines.append(f"  Worst trade:   {metrics['worst_trade_pct']:.1%}")
    lines.append(f"  Exposure adj:  {metrics['exposure_adjusted_return']:.2f}")
    lines.append(f"  vs null:       {metrics['benchmark_vs_null']:+.2%}")
    lines.append("")
    if metrics["closed_trades"] >= 20:
        lines.append("  LAST 20:")
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
