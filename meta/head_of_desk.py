"""meta/head_of_desk.py — Daily briefing generator and desk query engine.

Replaces the old auto-spawner (ensure_agent_count / cull_if_overpopulated)
with two core capabilities:

  1. generate_morning_brief() — builds a structured daily briefing covering
     desk P&L, agent-level metrics, regime breakdown, and actionable alerts.
  2. run_desk_query() — template-based natural-language query over the
     trade bank (win rates, best performers, regime analysis, etc.).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from store.performance import compute_metrics
from store.query import query_trades, query_win_rate, query_all_agents, summarize

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Kept helpers (population management removed) ─────────────────────


def get_agent_roster(conn) -> list[dict[str, Any]]:
    """Return a list of all non-benchmark agents with basic stats."""
    rows = conn.execute(
        """SELECT a.id, a.name, a.status, a.config_json,
                  a.spawn_date,
                  COALESCE(SUM(t.pnl_usd), 0) AS total_pnl,
                  COUNT(t.id) AS closed_trades
           FROM agents a
           LEFT JOIN trades t ON t.agent_id = a.id AND t.status = 'closed' AND t.voided = 0
           WHERE a.id NOT LIKE 'benchmark_%'
           GROUP BY a.id
           ORDER BY a.name"""
    ).fetchall()

    roster = []
    for row in rows:
        roster.append({
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "config_json": row["config_json"],
            "spawn_date": row["spawn_date"],
            "total_pnl": float(row["total_pnl"]),
            "closed_trades": int(row["closed_trades"]),
        })
    return roster


def get_strategy_distribution(conn) -> dict[str, int]:
    """Count active agents by strategy/persona type."""
    rows = conn.execute(
        "SELECT config_json FROM agents WHERE status NOT IN ('terminated', 'culled')"
    ).fetchall()

    distribution: dict[str, int] = {}
    for row in rows:
        strategy = "unknown"
        if row["config_json"]:
            try:
                pc = json.loads(row["config_json"])
                strategy = pc.get("strategy", pc.get("persona", "unknown"))
            except (json.JSONDecodeError, TypeError):
                strategy = "unknown"

        distribution[strategy] = distribution.get(strategy, 0) + 1
    return distribution


# ── Morning Brief ────────────────────────────────────────────────────


def generate_morning_brief(conn, config: dict | None = None) -> dict[str, Any]:
    """Build a structured daily briefing for the desk.

    Reads all active agents' performance via compute_metrics and returns a
    dict with keys: briefing_text, generated_at, agents_covered, summary.
    """
    now = _now()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    agent_rows = conn.execute(
        """SELECT id, name, status FROM agents
           WHERE id NOT LIKE 'benchmark_%'
           AND status NOT IN ('terminated', 'culled')
           ORDER BY name"""
    ).fetchall()

    agents_data: list[dict[str, Any]] = []
    desk_total_pnl = 0.0
    desk_total_trades = 0
    alerts: list[str] = []

    for row in agent_rows:
        aid = row["id"]
        metrics = compute_metrics(conn, aid)

        account = conn.execute(
            "SELECT balance, peak_balance FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
            (aid,),
        ).fetchone()
        balance = float(account["balance"]) if account else 50000.0
        peak = float(account["peak_balance"]) if account else 50000.0
        max_dd = (peak - balance) / peak if peak > 0 else 0.0

        agent_entry = {
            "id": aid,
            "name": row["name"],
            "status": row["status"],
            "metrics": metrics,
            "balance": round(balance, 2),
            "max_drawdown": round(max_dd, 4),
        }
        agents_data.append(agent_entry)

        desk_total_pnl += metrics.get("benchmark_vs_null", 0.0)
        desk_total_trades += metrics.get("closed_trades", 0)

        # ── Alerts ──
        if max_dd > 0.15:
            alerts.append(
                f"⚠ DRAWDOWN: {aid} at {max_dd:.1%} drawdown (>15% threshold)"
            )
        if metrics["closed_trades"] >= 20 and metrics["win_rate"] < 0.40:
            alerts.append(
                f"⚠ WIN RATE: {aid} at {metrics['win_rate']:.1%} win rate "
                f"after {metrics['closed_trades']} trades"
            )
        if metrics["last_7d_return"] < -0.05:
            alerts.append(
                f"⚠ WEEKLY LOSS: {aid} returned {metrics['last_7d_return']:+.1%} "
                f"over last 7 days"
            )
        if metrics["closed_trades"] >= 50 and metrics["profit_factor"] < 0.8:
            alerts.append(
                f"⚠ LOW PF: {aid} profit factor {metrics['profit_factor']:.2f} "
                f"below 0.8 after {metrics['closed_trades']} trades"
            )

    distribution = get_strategy_distribution(conn)

    all_trades = query_all_agents(conn)
    agg = summarize(all_trades)
    regime_wr: dict[str, list[bool]] = {}
    for t in all_trades:
        regime = t.get("regime") or "unknown"
        if regime not in regime_wr:
            regime_wr[regime] = []
        regime_wr[regime].append(t.get("result") == "win")
    regime_summary = {
        k: round(sum(v) / len(v), 4) if v else 0.0
        for k, v in regime_wr.items()
    }

    pending_reviews = [
        a["id"] for a in agents_data if a["status"] in ("suspended", "rookie")
    ]

    # M11.10 — diversity metric for the briefing
    diversity_data = None
    try:
        from meta.spawner import desk_diversity, check_immigration_quota  # noqa: PLC0415
        diversity_data = desk_diversity(conn)
        immigration = check_immigration_quota(conn)
    except Exception:
        logger.warning("briefing: could not compute diversity", exc_info=True)
        immigration = False

    # Regime alerts: consume the risk officer's persisted regime memo
    # (M9 crit 7a) — the briefing is its primary downstream reader.
    regime_memo = None
    try:
        from meta.risk_officer import RiskOfficer
        regime_memo = RiskOfficer.latest_regime_memo(conn)
    except Exception:
        logger.warning("briefing: could not read regime memo", exc_info=True)

    lines = [
        f"═══ FORGE DAILY BRIEFING — {today} ═══",
        "",
        f"Desk total P&L (vs null): {desk_total_pnl:+.2%}",
        f"Total closed trades: {desk_total_trades}",
        f"Active agents: {len(agents_data)}",
        f"Strategy mix: {json.dumps(distribution)}",
        "",
    ]

    try:
        from web.app import (
            compute_desk_equity,
            compute_null_band,
            compute_desk_deflated_sharpe,
            compute_hypothesis_validation_rate,
            get_diversity_score,
        )
        sb_equity = compute_desk_equity(conn)
        sb_null_band = compute_null_band(conn)
        sb_deflated = compute_desk_deflated_sharpe(conn)
        sb_validation = compute_hypothesis_validation_rate(conn)
        sb_diversity = get_diversity_score(conn)

        vr_pct = sb_validation * 100
        if sb_validation > 0.6:
            vr_label = "LEARNING WELL"
        elif sb_validation >= 0.4:
            vr_label = "MIXED"
        else:
            vr_label = "STRUGGLING"

        lines.extend([
            "── DESK SCOREBOARD ──",
            f"  Desk equity:       ${sb_equity:>12,.2f}",
            f"  Deflated Sharpe:   {sb_deflated:+.4f}",
            f"  Null band (p25/p50/p75): {sb_null_band['p25']:+.4f} / {sb_null_band['p50']:+.4f} / {sb_null_band['p75']:+.4f}",
            f"  Hypothesis rate:   {vr_pct:.1f}% ({vr_label})",
            f"  Diversity score:   {sb_diversity:.4f}",
            "",
        ])

        if sb_validation < 0.4:
            alerts.append(
                f"⚠ VALIDATION RATE: {vr_pct:.1f}% — agents are struggling to validate hypotheses"
            )
        if sb_diversity < 0.25:
            alerts.append(
                f"⚠ LOW DIVERSITY: {sb_diversity:.3f} — strategy overlap is high, risk of groupthink"
            )
    except Exception:
        logger.warning("briefing: could not compute scoreboard metrics", exc_info=True)

    if regime_memo:
        lines.append("── REGIME (risk officer memo) ──")
        lines.append(
            f"  tag={regime_memo.get('regime_tag', '?')}  "
            f"vol={regime_memo.get('average_volatility', '?')}  "
            f"funding={regime_memo.get('average_funding', '?')}  "
            f"fear={regime_memo.get('crypto_fear_index', '?')}"
        )
        lines.append("")

    lines.append("── AGENT SCOREBOARD ──")
    for a in sorted(agents_data, key=lambda x: x["metrics"].get("sharpe", 0), reverse=True):
        m = a["metrics"]
        pf = m["profit_factor"]
        pf_str = f"{pf:.2f}" if pf < 10 else "10.0+"
        lines.append(
            f"  {a['name']:20s}  status={a['status']:10s}  "
            f"WR={m['win_rate']:.0%}  PF={pf_str:>6s}  "
            f"Sharpe={m['sharpe']:+.2f}  "
            f"bal=${a['balance']:>10,.2f}  DD={a['max_drawdown']:.1%}"
        )

    if regime_summary:
        lines.append("")
        lines.append("── REGIME PERFORMANCE ──")
        for regime, wr in sorted(regime_summary.items()):
            lines.append(f"  {regime:20s}  WR={wr:.0%}")

    if alerts:
        lines.append("")
        lines.append("── ALERTS ──")
        for alert in alerts:
            lines.append(f"  {alert}")
    else:
        lines.append("")
        lines.append("── ALERTS ──")
        lines.append("  No alerts — desk within all thresholds.")

    # Pending reviews
    if pending_reviews:
        lines.append("")
        lines.append("── PENDING REVIEWS ──")
        for pid in pending_reviews:
            lines.append(f"  {pid}")

    if diversity_data:
        lines.append("")
        lines.append("── DESK DIVERSITY ──")
        lines.append(
            f"  Agents: {diversity_data['agent_count']}  "
            f"Avg Jaccard: {diversity_data['avg_jaccard']:.3f}  "
            f"Min Jaccard: {diversity_data['min_jaccard']:.3f}"
        )
        if diversity_data.get("coverage_by_family"):
            for fam, cov in sorted(diversity_data["coverage_by_family"].items()):
                lines.append(f"  {fam:20s}  coverage={cov:.0%}")
        lines.append(f"  Immigration required: {'YES' if immigration else 'no'}")

    briefing_text = "\n".join(lines)

    return {
        "briefing_text": briefing_text,
        "generated_at": now,
        "date": today,
        "agents_covered": [a["id"] for a in agents_data],
        "summary": {
            "desk_total_pnl": round(desk_total_pnl, 4),
            "total_trades": desk_total_trades,
            "agent_count": len(agents_data),
            "strategy_distribution": distribution,
            "regime_summary": regime_summary,
            "alert_count": len(alerts),
            "pending_reviews": pending_reviews,
            "diversity": diversity_data,
            "immigration_required": immigration,
        },
        "agents_data": agents_data,
        "regime_memo": regime_memo,
    }


def daily_briefing(conn, config: dict | None = None) -> str:
    """M9.11 API: return the briefing text as a plain string."""
    brief = generate_morning_brief(conn, config)
    return brief.get("briefing_text", "")


def store_briefing(conn, briefing: dict[str, Any]) -> None:
    """Persist a briefing dict into the briefings table.

    The briefings table schema: (id, date, content, created_at).
    ``content`` stores the full briefing dict as JSON text.
    """
    conn.execute(
        "INSERT INTO briefings (date, content, created_at) VALUES (?, ?, ?)",
        (
            briefing.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            json.dumps(briefing, default=str),
            briefing.get("generated_at", _now()),
        ),
    )
    conn.commit()
    logger.info("Stored briefing for %s", briefing.get("date"))


# ── Desk Query Engine ────────────────────────────────────────────────

# Pattern catalog: each entry is (compiled_regex, handler_function_name)
_QUERY_PATTERNS: list[tuple[re.Pattern[str], str]] = []


def _pat(pattern: str, handler: str) -> None:
    _QUERY_PATTERNS.append((re.compile(pattern, re.IGNORECASE), handler))


# ── Win-rate queries ─────────────────────────────────────────────────
_pat(r"(?:show|what(?:'s| is))\s+(?:me\s+)?(.+?)['']?s?\s+win\s*rate", "_handle_agent_win_rate")
_pat(r"(?:win\s*rate|wr)\s+(?:for\s+)?(.+)", "_handle_agent_win_rate")
_pat(r"(.+?)['']?s?\s+(?:win\s*rate|wr)", "_handle_agent_win_rate")

# ── Best / worst performers ─────────────────────────────────────────
_pat(r"what(?:'s| is)\s+working\s+best", "_handle_best_performers")
_pat(r"(?:best|top)\s+(?:performers?|agents?|traders?)", "_handle_best_performers")
_pat(r"who(?:'s| is)\s+(?:doing|performing)\s+best", "_handle_best_performers")
_pat(r"which\s+agents?\s+are\s+(?:losing|underperforming|worst)", "_handle_worst_performers")
_pat(r"(?:worst|bottom)\s+(?:performers?|agents?|traders?)", "_handle_worst_performers")
_pat(r"who(?:'s| is)\s+(?:losing|doing\s+poorly)", "_handle_worst_performers")

# ── Regime queries ───────────────────────────────────────────────────
_pat(r"trades?\s+(?:in|under)\s+(?:regime\s+)?['\"]?(\w+)['\"]?", "_handle_regime_trades")
_pat(r"(?:regime|market)\s+(?:analysis|breakdown|summary|performance)", "_handle_regime_breakdown")
_pat(r"how(?:'s| is)\s+(?:the\s+)?(?:desk\s+)?doing\s+(?:in|under)\s+(\w+)", "_handle_regime_trades")

# ── Desk summary ─────────────────────────────────────────────────────
_pat(r"(?:desk\s+)?(?:summary|overview|status|health)", "_handle_desk_summary")
_pat(r"how(?:'s| is)\s+(?:the\s+)?desk\s+(?:doing|looking)", "_handle_desk_summary")
_pat(r"what(?:'s| is)\s+(?:the\s+)?desk\s+(?:p.?l|pnl|profit|loss)", "_handle_desk_summary")

# ── Trade count / query ──────────────────────────────────────────────
_pat(r"(?:how\s+many|total)\s+trades?", "_handle_trade_count")
_pat(r"trades?\s+(?:for|by)\s+(.+)", "_handle_agent_trades")
_pat(r"recent\s+trades", "_handle_recent_trades")
_pat(r"show\s+(?:me\s+)?(?:the\s+)?recent", "_handle_recent_trades")

# ── Agent count / roster ─────────────────────────────────────────────
_pat(r"(?:how\s+many|count)\s+agents?", "_handle_agent_count")
_pat(r"(?:list|show)\s+(?:me\s+)?(?:the\s+)?agents?", "_handle_agent_roster")
_pat(r"(?:who(?:'re| are))\s+(?:the\s+)?agents?", "_handle_agent_roster")


# ── Handlers ─────────────────────────────────────────────────────────

def _handle_agent_win_rate(conn: Any, match: re.Match[str]) -> str:
    agent_name = match.group(1).strip().lower().replace(" ", "_")
    agent_id = _fuzzy_find_agent(conn, agent_name)
    if not agent_id:
        return f"Unknown agent: {agent_name}. Use 'list agents' to see available agents."

    stats = query_win_rate(conn, {"agent_id": agent_id})
    return (
        f"Win rate for {agent_id}:\n"
        f"  Win rate:   {stats['win_rate']:.1%}\n"
        f"  Total:      {stats['total_trades']} closed trades\n"
        f"  Profit factor: {stats['profit_factor']:.2f}"
    )


def _handle_best_performers(conn: Any, _match: re.Match[str]) -> str:
    rows = conn.execute(
        """SELECT a.id, COALESCE(SUM(t.pnl_usd), 0) AS total_pnl,
                  COUNT(t.id) AS trades
           FROM agents a
           LEFT JOIN trades t ON t.agent_id = a.id AND t.status = 'closed' AND t.voided = 0
           WHERE a.id NOT LIKE 'benchmark_%'
           AND a.status NOT IN ('terminated', 'culled')
           GROUP BY a.id
           ORDER BY total_pnl DESC
           LIMIT 5"""
    ).fetchall()

    if not rows:
        return "No agent data available yet."

    lines = ["Top performers (by total P&L):"]
    for i, r in enumerate(rows, 1):
        pnl = float(r["total_pnl"])
        mark = "+" if pnl >= 0 else ""
        lines.append(
            f"  {i}. {r['id']:20s}  PnL={mark}{pnl:.2f}  trades={r['trades']}"
        )
    return "\n".join(lines)


def _handle_worst_performers(conn: Any, _match: re.Match[str]) -> str:
    rows = conn.execute(
        """SELECT a.id, COALESCE(SUM(t.pnl_usd), 0) AS total_pnl,
                  COUNT(t.id) AS trades
           FROM agents a
           LEFT JOIN trades t ON t.agent_id = a.id AND t.status = 'closed' AND t.voided = 0
           WHERE a.id NOT LIKE 'benchmark_%'
           AND a.status NOT IN ('terminated', 'culled')
           GROUP BY a.id
           ORDER BY total_pnl ASC
           LIMIT 5"""
    ).fetchall()

    if not rows:
        return "No agent data available yet."

    lines = ["Underperformers (by total P&L):"]
    for i, r in enumerate(rows, 1):
        pnl = float(r["total_pnl"])
        mark = "+" if pnl >= 0 else ""
        lines.append(
            f"  {i}. {r['id']:20s}  PnL={mark}{pnl:.2f}  trades={r['trades']}"
        )
    return "\n".join(lines)


def _handle_regime_trades(conn: Any, match: re.Match[str]) -> str:
    regime = match.group(1).strip().lower()
    trades = query_trades(conn, regime=regime, decode_ohlcv=False, limit=20)
    agg = summarize(trades)
    if agg["closed_count"] == 0:
        return f"No closed trades found for regime '{regime}'."

    return (
        f"Regime '{regime}':\n"
        f"  Total trades:  {agg['closed_count']}\n"
        f"  Win rate:      {agg['win_rate']:.1%}\n"
        f"  Avg PnL:       {agg['avg_pnl_pct']:+.2%}\n"
        f"  Wins / Losses: {agg['wins']} / {agg['losses']}"
    )


def _handle_regime_breakdown(conn: Any, _match: re.Match[str]) -> str:
    all_trades = query_all_agents(conn)
    if not all_trades:
        return "No trades in the bank yet."

    regime_data: dict[str, list[dict]] = {}
    for t in all_trades:
        regime = t.get("regime") or "unknown"
        if regime not in regime_data:
            regime_data[regime] = []
        regime_data[regime].append(t)

    lines = ["Regime breakdown (desk-wide):"]
    for regime in sorted(regime_data):
        agg = summarize(regime_data[regime])
        lines.append(
            f"  {regime:20s}  trades={agg['closed_count']:4d}  "
            f"WR={agg['win_rate']:.0%}  avg_pnl={agg['avg_pnl_pct']:+.2%}"
        )
    return "\n".join(lines)


def _handle_desk_summary(conn: Any, _match: re.Match[str]) -> str:
    all_trades = query_all_agents(conn)
    agg = summarize(all_trades)

    agent_count = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE id NOT LIKE 'benchmark_%' "
        "AND status NOT IN ('terminated', 'culled')"
    ).fetchone()[0]

    total_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM trades "
        "WHERE status = 'closed' AND voided = 0"
    ).fetchone()[0]

    total_balance = conn.execute(
        "SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE mode = 'paper'"
    ).fetchone()[0]

    return (
        f"Desk summary:\n"
        f"  Agents:        {agent_count}\n"
        f"  Total trades:  {agg['closed_count']}\n"
        f"  Desk win rate: {agg['win_rate']:.1%}\n"
        f"  Total P&L:     ${total_pnl:+,.2f}\n"
        f"  Desk balance:  ${total_balance:,.2f}"
    )


def _handle_trade_count(conn: Any, _match: re.Match[str]) -> str:
    total = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status = 'closed' AND voided = 0"
    ).fetchone()[0]
    open_count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status = 'open' AND voided = 0"
    ).fetchone()[0]
    return f"Total trades: {total} closed, {open_count} open."


def _handle_agent_trades(conn: Any, match: re.Match[str]) -> str:
    agent_name = match.group(1).strip().lower().replace(" ", "_")
    agent_id = _fuzzy_find_agent(conn, agent_name)
    if not agent_id:
        return f"Unknown agent: {agent_name}."

    trades = query_trades(conn, agent_id=agent_id, decode_ohlcv=False, limit=10)
    if not trades:
        return f"No trades found for {agent_id}."

    lines = [f"Recent trades for {agent_id}:"]
    for t in trades[:10]:
        pnl = t.get("pnl_pct", 0) or 0
        lines.append(
            f"  {t.get('asset', '?'):10s} {t.get('direction', '?'):5s} "
            f"PnL={pnl:+.2%}  {t.get('result', t.get('status', '?'))}"
        )
    return "\n".join(lines)


def _handle_recent_trades(conn: Any, _match: re.Match[str]) -> str:
    trades = query_all_agents(conn)
    if not trades:
        return "No trades in the bank yet."

    recent = sorted(
        trades,
        key=lambda t: t.get("entry_timestamp", ""),
        reverse=True,
    )[:10]

    lines = ["Recent trades (across all agents):"]
    for t in recent:
        pnl = t.get("pnl_pct", 0) or 0
        lines.append(
            f"  {t.get('agent_id', '?'):16s} {t.get('asset', '?'):10s} "
            f"{t.get('direction', '?'):5s} PnL={pnl:+.2%}  "
            f"{t.get('result', t.get('status', '?'))}"
        )
    return "\n".join(lines)


def _handle_agent_count(conn: Any, _match: re.Match[str]) -> str:
    counts = conn.execute(
        """SELECT status, COUNT(*) as cnt FROM agents
           WHERE id NOT LIKE 'benchmark_%'
           GROUP BY status ORDER BY status"""
    ).fetchall()
    total = sum(r["cnt"] for r in counts)
    parts = [f"{r['status']}={r['cnt']}" for r in counts]
    return f"Total agents: {total} ({', '.join(parts)})"


def _handle_agent_roster(conn: Any, _match: re.Match[str]) -> str:
    roster = get_agent_roster(conn)
    if not roster:
        return "No agents in the roster."

    lines = ["Agent roster:"]
    for a in roster:
        lines.append(
            f"  {a['name']:20s}  status={a['status']:10s}  "
            f"trades={a['closed_trades']}  pnl=${a['total_pnl']:+,.2f}"
        )
    return "\n".join(lines)


# ── Utilities ────────────────────────────────────────────────────────

def _fuzzy_find_agent(conn: Any, query: str) -> str | None:
    """Match a query string against agent IDs with fuzzy tolerance."""
    row = conn.execute(
        "SELECT id FROM agents WHERE id = ? AND id NOT LIKE 'benchmark_%'",
        (query,),
    ).fetchone()
    if row:
        return row["id"]

    row = conn.execute(
        "SELECT id FROM agents WHERE LOWER(id) LIKE ? AND id NOT LIKE 'benchmark_%' LIMIT 1",
        (f"%{query.lower()}%",),
    ).fetchone()
    if row:
        return row["id"]

    normalized = query.replace("-", "_")
    row = conn.execute(
        "SELECT id FROM agents WHERE LOWER(REPLACE(id, '-', '_')) LIKE ? "
        "AND id NOT LIKE 'benchmark_%' LIMIT 1",
        (f"%{normalized.lower()}%",),
    ).fetchone()
    return row["id"] if row else None


def run_desk_query(conn: Any, query_text: str) -> str:
    """Execute a natural-language query against the trade bank.

    Uses template-based regex matching (no LLM required). Supports queries
    about agent win rates, best/worst performers, regime analysis, desk
    summary, trade counts, and more.

    Returns a formatted text summary.
    """
    text = query_text.strip()
    if not text:
        return "Please enter a query. Try: 'desk summary', 'what's working best', or 'win rate for jade_hawk'."

    for pattern, handler_name in _QUERY_PATTERNS:
        match = pattern.search(text)
        if match:
            handler = globals()[handler_name]
            try:
                return handler(conn, match)
            except Exception as exc:
                logger.error("Query handler %s failed: %s", handler_name, exc)
                return f"Error processing query: {exc}"

    return (
        "I didn't understand that query. Try:\n"
        "  - 'desk summary' — overall desk status\n"
        "  - 'what's working best' — top performers\n"
        "  - 'which agents are losing' — underperformers\n"
        "  - 'win rate for <agent>' — agent win rate\n"
        "  - 'trades in <regime>' — regime analysis\n"
        "  - 'recent trades' — latest trades\n"
        "  - 'list agents' — agent roster"
    )


# ── LLM-composed chat (with structured fallback) ─────────────────────

_CHAT_SYSTEM_PROMPT = (
    "You are the Head of Desk of Forge, an autonomous paper-trading desk. "
    "Answer the user's question using ONLY the query-tool results provided. "
    "Cite concrete agent names and numbers from the data. If the data does "
    "not answer the question, say so plainly. Keep the answer under 150 words."
)


def compose_chat_answer(conn: Any, query_text: str, llm_fn=None) -> str:
    """Answer a desk chat question: retrieval via the query tools
    (run_desk_query), then optional LLM composition over the retrieved
    data. Falls back to the structured tool answer whenever the LLM is
    unavailable, errors, or returns nothing — chat must never hard-fail
    because the model is down.

    llm_fn has the reflection-transport contract
    (system_prompt, user_prompt) -> str; when None the caller opted out
    of LLM composition (or pass llm.reflection_client.complete).
    """
    tool_answer = run_desk_query(conn, query_text)

    if llm_fn is None:
        return tool_answer

    user_prompt = (
        f"Question from the desk owner:\n{query_text}\n\n"
        f"Query-tool results (ground truth — cite from this):\n{tool_answer}"
    )
    try:
        composed = llm_fn(_CHAT_SYSTEM_PROMPT, user_prompt)
    except Exception:
        logger.warning("chat LLM composition failed; using tool answer", exc_info=True)
        return tool_answer

    composed = (composed or "").strip()
    return composed if composed else tool_answer


# ── Chat history (chat_history table) ────────────────────────────────


def save_chat_turn(conn: Any, role: str, content: str) -> None:
    """Persist one chat turn. role ∈ {'user', 'assistant'}."""
    conn.execute(
        "INSERT INTO chat_history (role, content, created_at) VALUES (?, ?, ?)",
        (role, content, _now()),
    )
    conn.commit()


def get_chat_history(conn: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent chat turns, oldest first."""
    rows = conn.execute(
        "SELECT role, content, created_at FROM chat_history "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        for r in reversed(rows)
    ]
