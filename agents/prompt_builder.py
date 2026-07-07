from store.db import get_trades, get_positions, get_latest_account
from store.performance import compute_metrics, format_performance_summary
from store.query import query_trades, summarize
from store.positions import get_desk_positions_summary

# Hard requirement from the captain: agents must never assume they can see or
# react to price movement faster than the heartbeat's own cadence.
MARKET_DATA_CADENCE_NOTICE = (
    "Market data refreshes every 5 minutes; you cannot see or act on price "
    "movements faster than this. Do not assume intraday granularity finer "
    "than 5 minutes when reasoning about entries or exits."
)


def build_portfolio_snapshot(conn, agent_id: str, config: dict | None = None) -> dict:
    """Per-agent portfolio-level snapshot at the current moment: cash/equity,
    exposure, open positions (count/list), PnL, and risk utilization.

    This is the same category of data the decision prompt's Portfolio
    section already assembles (account balance/peak/drawdown, open
    positions, performance metrics) — factored out here so it can also be
    captured as-is into a trade fingerprint's `market_context.portfolio`
    block (see agents/decision_loop.py) without recomputing it differently
    in two places.
    """
    desk_config = (config or {}).get("desk", {})
    starting_balance = desk_config.get("starting_balance", 50000.0)

    account = get_latest_account(conn, agent_id, "paper") or {
        "balance": starting_balance,
        "peak_balance": starting_balance,
    }
    balance = account["balance"]
    peak = account["peak_balance"]
    dd_pct = (peak - balance) / peak if peak > 0 else 0.0

    metrics = compute_metrics(conn, agent_id)
    open_positions = get_positions(conn, agent_id)

    exposure_usd = sum(p.get("notional_usd", 0) or 0 for p in open_positions)
    unrealized_pnl_pct = sum(p.get("current_pnl_pct", 0) or 0 for p in open_positions)

    max_concurrent = desk_config.get("max_concurrent_positions")
    position_utilization = (
        len(open_positions) / max_concurrent if max_concurrent else None
    )

    return {
        "cash": balance,
        "equity": balance,
        "peak_balance": peak,
        "drawdown_pct": dd_pct,
        "exposure_usd": exposure_usd,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "open_position_count": len(open_positions),
        "open_positions": open_positions,
        "performance": metrics,
        "risk_utilization": {
            "open_positions": len(open_positions),
            "max_concurrent_positions": max_concurrent,
            "position_utilization_pct": position_utilization,
        },
    }


async def build_decision_prompt(
    agent_id: str,
    thesis_text: str,
    heartbeat: dict,
    conn,
    provider,
    starting_balance: float = 50000.0,
    universe: list[str] | None = None,
) -> str:
    account = get_latest_account(conn, agent_id, "paper") or {
        "balance": starting_balance,
        "peak_balance": starting_balance,
    }
    balance = account["balance"]
    peak = account["peak_balance"]
    dd_pct = (peak - balance) / peak if peak > 0 else 0.0

    metrics = compute_metrics(conn, agent_id)
    perf_summary = format_performance_summary(metrics, agent_id)

    open_positions = get_positions(conn, agent_id)

    closed_trades = get_trades(conn, agent_id, limit=10)
    closed_only = [t for t in closed_trades if t.get("status") == "closed"]
    trade_lines = []
    for t in closed_only[:10]:
        pnl = t.get("pnl_pct", 0) or 0
        postmortem = t.get("agent_postmortem", "")
        pm = f" — {postmortem[:80]}" if postmortem else ""
        trade_lines.append(
            f"  {t['asset']} {t['direction']} | PnL: {pnl:+.2%} | "
            f"exit: {t.get('exit_reason', '?')}{pm}"
        )
    trades_section = (
        "\n".join(trade_lines) if trade_lines else "  No closed trades yet."
    )

    pos_lines = []
    for p in open_positions:
        pos_lines.append(
            f"  {p['asset']} {p['direction']} @ {p['entry_price']:.4f} | "
            f"SL: {p['stop_loss_price']:.4f} | TP: {p['take_profit_price']:.4f}"
        )
    positions_section = "\n".join(pos_lines) if pos_lines else "  No open positions."

    desk_positions = get_desk_positions_summary(conn, exclude_agent_id=agent_id)

    assets_data = heartbeat.get("assets", {})
    tracked_universe = universe if universe is not None else list(assets_data.keys())
    sorted_assets = sorted(
        ((a, assets_data[a]) for a in tracked_universe if a in assets_data),
        key=lambda kv: abs(kv[1].get("funding") or 0),
        reverse=True,
    )
    market_lines = _build_full_asset_blocks(sorted_assets)
    market_section = (
        "\n".join(market_lines) if market_lines else "  No market data available."
    )

    cross_asset = heartbeat.get("cross_asset", {})
    sector_strength = cross_asset.get("sector_strength") or {}
    sector_str = ", ".join(
        f"{sector}={val:+.2%}" for sector, val in sector_strength.items() if val is not None
    ) or "n/a"
    corr_matrix = cross_asset.get("correlation_matrix")
    corr_str = _format_corr_matrix(corr_matrix) if corr_matrix else "n/a"
    cross_section = (
        f"  Breadth (24h, pct assets up): {cross_asset.get('market_breadth', 0):.0%} | "
        f"Leader: {cross_asset.get('leader') or '?'} | Laggard: {cross_asset.get('laggard') or '?'}\n"
        f"  Sector strength (24h avg return): {sector_str}\n"
        f"  Correlation matrix:\n{corr_str}"
    )

    regime_obj = heartbeat.get("regime", {})
    regime = regime_obj.get("regime_tag", "range_low_vol")
    regime_section = (
        f"  Tag: {regime} | Risk-on score: {regime_obj.get('risk_on_score', 0):.2f} | "
        f"Trend score: {regime_obj.get('trend_score', 0):.2f} | "
        f"Fear & Greed index: {regime_obj.get('crypto_fear_index')}"
    )

    heartbeat_ts = heartbeat.get("timestamp", "unknown")
    top_asset = sorted_assets[0][0] if sorted_assets else None
    trade_bank_section = _build_trade_bank_section(conn, agent_id, regime, top_asset)
    derived_section = _build_derived_features_section(sorted_assets)

    return f"""=== YOUR THESIS ===
{thesis_text}

{perf_summary}

Account: ${balance:,.2f} | Peak: ${peak:,.2f} | Current DD: {dd_pct:.1%}

=== LAST 10 CLOSED TRADES ===
{trades_section}

=== YOUR OPEN POSITIONS ===
{positions_section}

=== DESK POSITIONS (other traders) ===
{desk_positions}

=== MARKET DATA CADENCE (as of {heartbeat_ts}) ===
{MARKET_DATA_CADENCE_NOTICE}

=== MARKET REGIME ===
{regime_section}

=== CROSS-ASSET OVERVIEW ===
{cross_section}

=== MARKET STATE (your {len(sorted_assets)} tracked assets) ===
{market_section}

{derived_section}

{trade_bank_section}

=== DECISION ===
Based on your thesis, your performance record, and current market conditions, make a decision.

IMPORTANT: You reason in probabilities, not checklists. Every signal has strength, not just presence.
- confidence (0.0-1.0): Your overall conviction in this trade. Below 0.50 is a firm veto — wait.
- evidence_strength: Per-signal factor scores from -1.0 to +1.0 (sign = direction, magnitude = strength).
  Missing data reduces confidence but does not veto a trade automatically.
- uncertainty_factors: List specific factors increasing uncertainty (e.g. "orderbook unavailable reduces conviction").

You may:
  - Enter a new trade: {{"action": "enter", "asset": "...", "direction": "long|short", "entry_price": 0.0, "stop_loss_price": 0.0, "take_profit_price": 0.0, "leverage": 1, "position_size_pct": 0.10, "hypothesis": "...", "key_conditions_met": [], "key_conditions_missing": [], "confidence": 0.72, "evidence_strength": {{"funding": 0.6, "oi": 0.3, "momentum": -0.2, "volatility": 0.4}}, "uncertainty_factors": ["orderbook depth thinning reduces conviction"], "expected_value": "..."}}
  - Wait: {{"action": "wait", "reason": "...", "confidence": 0.35, "evidence_strength": {{"funding": 0.1, "oi": -0.2}}, "uncertainty_factors": []}}
  - Close a position: {{"action": "close", "position_id": "...", "reason": "..."}}

Wait decisions are logged and scored for calibration exactly like entries -- report your real conviction and evidence, not just a reason string. A well-calibrated 0.35 that correctly stayed out is as valuable to your track record as a well-calibrated 0.72 that entered.

Output JSON only."""


def _build_trade_bank_section(
    conn, agent_id: str, regime: str, top_asset: str | None
) -> str:
    """Trade bank query section: the agent's own recent trades under similar
    conditions (same asset OR same regime), plus a cross-agent pattern
    reference for the most active asset right now. Queries store/query.py.
    """
    own_by_regime = query_trades(
        conn,
        agent_id=agent_id,
        regime=regime,
        status="closed",
        decode_ohlcv=False,
        limit=5,
    )
    own_by_asset = (
        query_trades(
            conn,
            agent_id=agent_id,
            asset=top_asset,
            status="closed",
            decode_ohlcv=False,
            limit=5,
        )
        if top_asset
        else []
    )
    similar = {t["id"]: t for t in own_by_regime + own_by_asset}
    similar_trades = sorted(
        similar.values(), key=lambda t: t.get("entry_timestamp") or "", reverse=True
    )[:5]

    lines = []
    for t in similar_trades:
        pnl = t.get("pnl_pct", 0) or 0
        lines.append(
            f"  {t['asset']} {t['direction']} | regime={t.get('regime') or '?'} | "
            f"PnL: {pnl:+.2%} | result={t.get('result') or '?'}"
        )
    own_summary = summarize(similar_trades)
    own_block = (
        "\n".join(lines)
        if lines
        else "  No matching trades yet (same asset or regime)."
    )

    cross_block = "  No cross-agent data yet."
    if top_asset:
        cross_trades = query_trades(
            conn,
            agent_id=None,
            asset=top_asset,
            status="closed",
            decode_ohlcv=False,
            limit=100,
        )
        cross_summary = summarize(cross_trades)
        if cross_summary["closed_count"]:
            cross_block = (
                f"  {top_asset} across the desk (all agents, closed trades): "
                f"{cross_summary['closed_count']} trades | "
                f"win rate {cross_summary['win_rate']:.0%} | "
                f"avg PnL {cross_summary['avg_pnl_pct']:+.2%}"
            )

    return f"""=== TRADE BANK — YOUR HISTORY UNDER SIMILAR CONDITIONS ===
{own_block}
  Win rate: {own_summary['win_rate']:.0%} ({own_summary['closed_count']} closed, same asset or regime)

=== CROSS-AGENT PATTERN REFERENCE ===
{cross_block}"""


def _build_derived_features_section(sorted_assets: list[tuple[str, dict]]) -> str:
    """Compact table of computed derived features per asset.
    Shows momentum_acceleration, atr_percentile, bb_width,
    volume_percentile_14d, and funding_acceleration where available.
    """
    if not sorted_assets:
        return ""
    lines = []
    for asset, data in sorted_assets:
        accel = data.get("momentum_acceleration")
        atr_pct = data.get("atr_percentile")
        bb_w = data.get("bb_width")
        vol_pct = data.get("volume_percentile_14d")
        f_accel = data.get("funding_acceleration")

        accel_str = f"{accel:+.4f}" if accel is not None else "  n/a  "
        atr_str = f"{atr_pct:.2f}" if atr_pct is not None else " n/a"
        bb_str = f"{bb_w:.4f}" if bb_w is not None else "  n/a "
        volp_str = f"{vol_pct:.2f}" if vol_pct is not None else " n/a"
        facc_str = f"{f_accel:+.4f}" if f_accel is not None else "  n/a  "

        lines.append(
            f"  {asset:12s} accel={accel_str} atr_pct={atr_str} "
            f"bb_w={bb_str} vol_pct={volp_str} fund_acc={facc_str}"
        )
    section = "\n".join(lines)
    return f"\n=== DERIVED FEATURES ===\n{section}"


def _pct(val, fallback="n/a"):
    if val is None:
        return fallback
    return f"{val:+.4%}"


def _n(val, fmt=".4f", fallback="n/a"):
    if val is None:
        return fallback
    return f"{val:{fmt}}"


def _build_full_asset_blocks(
    sorted_assets: list[tuple[str, dict]],
) -> list[str]:
    """All heartbeat per-asset fields in a compact 4-line block per asset."""
    blocks = []
    for asset, data in sorted_assets:
        price = _n(data.get("price"), ".2f")
        ret5 = _pct(data.get("return_5m"))
        ret30 = _pct(data.get("return_30m"))
        ret4h = _pct(data.get("return_4h"))
        ret24h = _pct(data.get("return_24h"))
        vol = _n(data.get("volume"), ".2f")
        oi = _n(data.get("open_interest"), ".1f")
        fund = _pct(data.get("funding") or 0.0)
        fund_z = _n(data.get("funding_zscore"), ".2f")
        oi_z = _n(data.get("oi_zscore"), ".2f")
        vol_z = _n(data.get("volume_zscore"), ".2f")
        vwap = _pct(data.get("vwap_distance"))
        atr = _n(data.get("atr"), ".1f")
        rsi = _n(data.get("rsi"), ".1f")
        rv = _n(data.get("realized_vol"), ".2f")
        spread = _pct(data.get("spread"))
        bd = _n(data.get("bid_depth"), ".2f")
        ad = _n(data.get("ask_depth"), ".2f")
        di = _n(data.get("depth_imbalance"), "+.2f")
        se = _pct(data.get("slippage_estimate"))
        bv = _n(data.get("buy_volume"), ".2f")
        sv = _n(data.get("sell_volume"), ".2f")
        ar = _n(data.get("aggressor_ratio"), ".3f")
        ats = _n(data.get("avg_trade_size"), ".4f")
        lt = _n(data.get("largest_trade"), ".1f")
        oi_dd = _pct(data.get("oi_drawdown_pct"))
        lt_vol = _n(data.get("large_trade_volume_usd"), ".1f")
        liq_flag = _n(data.get("liquidation_cascade_flag"), ".0f")

        blocks.append(
            f"  {asset:12s} price={price} ret5={ret5} ret30={ret30} "
            f"ret4h={ret4h} ret24h={ret24h} vol={vol} oi={oi}\n"
            f"             fund={fund} fund_z={fund_z} oi_z={oi_z} vol_z={vol_z} "
            f"vwap={vwap} atr={atr} rsi={rsi} rv={rv}\n"
            f"             spread={spread} bid={bd} ask={ad} "
            f"depth={di} slip={se} oi_dd={oi_dd}\n"
            f"             buy={bv} sell={sv} agg={ar} avg_trade={ats} "
            f"largest={lt} lt_vol={lt_vol} liq_flag={liq_flag} "
            f"candles_5m={_candle_summary(data.get('candles_5m'))}"
        )
    return blocks


def _candle_summary(candles, fallback="none"):
    if not candles:
        return fallback
    last = candles[-1]
    ts = last[0] if len(last) > 0 else "?"
    o, h, l, c = last[1], last[2], last[3], last[4]
    return f"{len(candles)}x5m o={o} h={h} l={l} c={c}"


def _format_corr_matrix(matrix: dict | None) -> str:
    if not matrix:
        return "  n/a"
    assets = sorted(matrix.keys())
    entries = []
    for i, a in enumerate(assets):
        for b in assets[i + 1:]:
            r = matrix[a].get(b)
            if r is not None:
                entries.append(f"{a}/{b}={r:.2f}")
    if not entries:
        return "  n/a"
    pairs = sorted(entries, key=lambda x: -abs(float(x.split("=")[1])))
    # Cap at most informative 15 pairs
    pairs = pairs[:15]
    rows = ["  " + "  ".join(pairs[i:i+3]) for i in range(0, len(pairs), 3)]
    return "\n".join(rows)
