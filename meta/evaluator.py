"""meta/evaluator.py — Per-agent metric assessment vs null model.

Statistical evaluation of agent performance against the benchmark_random_walk
null distribution. Provides significance testing, lifecycle decision
recommendations, and harvest candidate identification.
"""
from __future__ import annotations

import json
import logging
import random
import statistics
from datetime import datetime, timezone
from typing import Any

from store.performance import compute_metrics

logger = logging.getLogger(__name__)

BOOTSTRAP_RESAMPLES = 1000
NULL_MIN_TRADES = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_null_metrics(conn) -> dict[str, Any] | None:
    """Get metrics for the benchmark_random_walk agent as the null distribution."""
    row = conn.execute(
        "SELECT id FROM agents WHERE id = 'benchmark_random_walk'"
    ).fetchone()
    if not row:
        return None
    return compute_metrics(conn, "benchmark_random_walk")


def _get_null_per_trade_returns(conn) -> list[float]:
    """Load per-trade returns from the benchmark_random_walk agent."""
    rows = conn.execute(
        """SELECT pnl_pct FROM trades
           WHERE agent_id = 'benchmark_random_walk'
             AND status = 'closed' AND voided = 0
             AND pnl_pct IS NOT NULL
           ORDER BY entry_timestamp ASC"""
    ).fetchall()
    return [r["pnl_pct"] for r in rows]


def _compute_sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    avg = statistics.mean(returns)
    std = statistics.stdev(returns)
    return avg / std if std > 0 else 0.0


def significance_test(
    agent_metrics: dict[str, Any],
    null_metrics: dict[str, Any] | None,
    conn=None,
) -> dict[str, Any]:
    """Compare agent performance against the null distribution.

    Uses bootstrap resampling (1,000 resamples) of the benchmark_random_walk
    per-trade returns to build a null Sharpe distribution.  The agent's
    empirical Sharpe percentile is the p-value.

    Preserves R12 latch: if null has fewer than NULL_MIN_TRADES trades,
    returns None-significance (insufficient_data).

    Returns a dict with:
      - beats_null: bool
      - p_value_estimate: str ("<0.05", "<0.10", ">0.10", "insufficient_data")
      - sharpe_diff: float
      - profit_factor_diff: float
      - win_rate_diff: float
    """
    agent_trades = agent_metrics.get("closed_trades", 0)

    if agent_trades < 20 or null_metrics is None:
        return {
            "beats_null": False,
            "p_value_estimate": "insufficient_data",
            "sharpe_diff": 0.0,
            "profit_factor_diff": 0.0,
            "win_rate_diff": 0.0,
        }

    agent_sharpe = agent_metrics.get("sharpe", 0.0)
    null_sharpe = null_metrics.get("sharpe", 0.0)
    sharpe_diff = agent_sharpe - null_sharpe

    agent_pf = agent_metrics.get("profit_factor", 0.0)
    null_pf = null_metrics.get("profit_factor", 0.0)
    pf_diff = agent_pf - null_pf

    agent_wr = agent_metrics.get("win_rate", 0.0)
    null_wr = null_metrics.get("win_rate", 0.0)
    wr_diff = agent_wr - null_wr

    p_est = ">0.10"
    if agent_trades >= 30 and conn is not None:
        null_returns = _get_null_per_trade_returns(conn)
        null_trades = null_metrics.get("closed_trades", 0)
        if null_trades < NULL_MIN_TRADES:
            p_est = "insufficient_data"
        elif len(null_returns) >= 2:
            rng = random.Random(42)
            null_sharpes: list[float] = []
            for _ in range(BOOTSTRAP_RESAMPLES):
                sample = rng.choices(null_returns, k=agent_trades)
                null_sharpes.append(_compute_sharpe(sample))
            null_sharpes.sort()
            count_above = sum(1 for s in null_sharpes if s >= agent_sharpe)
            percentile = count_above / len(null_sharpes)
            if percentile < 0.05:
                p_est = "<0.05"
            elif percentile < 0.10:
                p_est = "<0.10"
            else:
                p_est = ">0.10"
    elif agent_trades >= 30:
        se = 1.0 / (agent_trades ** 0.5)
        t_stat = sharpe_diff / se if se > 0 else 0.0
        if t_stat > 1.96:
            p_est = "<0.05"
        elif t_stat > 1.28:
            p_est = "<0.10"
        else:
            p_est = ">0.10"
    else:
        p_est = "insufficient_data"

    beats = (
        agent_trades >= 30
        and sharpe_diff > 0
        and agent_sharpe > 0
        and p_est in ("<0.05", "<0.10")
    )

    return {
        "beats_null": beats,
        "p_value_estimate": p_est,
        "sharpe_diff": round(sharpe_diff, 4),
        "profit_factor_diff": round(pf_diff, 4),
        "win_rate_diff": round(wr_diff, 4),
    }


def get_lifecycle_decision(
    conn,
    agent_id: str,
    metrics: dict[str, Any],
    null_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """Determine the lifecycle decision for an agent based on its metrics.

    Returns a dict with keys:
      - decision: str ("active", "suspend", "terminate", "review")
      - reason: str
      - trigger: str (which rule triggered)
    """
    agent = conn.execute(
        "SELECT * FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()

    if agent is None:
        return {"decision": "terminate", "reason": "agent not found", "trigger": "not_found"}

    agent_status = agent["status"]
    total_trades = metrics.get("closed_trades", 0)
    win_rate = metrics.get("win_rate", 0.0)
    profit_factor = metrics.get("profit_factor", 0.0)

    # Get max drawdown from account records
    account = conn.execute(
        "SELECT balance, peak_balance FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    max_dd = 0.0
    if account and account["peak_balance"] > 0:
        max_dd = (account["peak_balance"] - account["balance"]) / account["peak_balance"]

    # Check zero-trades-in-5-days
    if total_trades > 0:
        last_trade = conn.execute(
            "SELECT entry_timestamp FROM trades WHERE agent_id = ? AND status = 'closed' ORDER BY entry_timestamp DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if last_trade and last_trade["entry_timestamp"]:
            try:
                last_ts = datetime.fromisoformat(
                    last_trade["entry_timestamp"].replace("Z", "+00:00")
                )
                days_since = (datetime.now(timezone.utc) - last_ts).days
                if days_since >= 5:
                    return {
                        "decision": "review",
                        "reason": f"No trades in {days_since} days (thesis review required)",
                        "trigger": "zero_trades_5d",
                    }
            except (ValueError, TypeError):
                pass

    # Immediate termination: win rate < 35% after 50 trades
    if win_rate < 0.35 and total_trades >= 50:
        return {
            "decision": "terminate",
            "reason": f"Win rate {win_rate:.1%} below 35% after {total_trades} trades",
            "trigger": "win_rate_below_35",
        }

    # Immediate suspension: drawdown > 20%
    if max_dd > 0.20:
        return {
            "decision": "suspend",
            "reason": f"Max drawdown {max_dd:.1%} exceeds 20%",
            "trigger": "drawdown_exceeds_20pct",
        }

    # Suspension: PF < 0.8 for two consecutive evaluation cycles
    # Check the last two evaluations
    evals = conn.execute(
        """SELECT decision, metrics_json FROM evaluations
           WHERE agent_id = ? ORDER BY id DESC LIMIT 2""",
        (agent_id,),
    ).fetchall()

    if profit_factor < 0.8 and total_trades >= 20:
        if len(evals) >= 2:
            # Check if previous evaluation also had low PF
            try:
                prev_metrics = json.loads(evals[1]["metrics_json"])
                prev_pf = prev_metrics.get("profit_factor", 1.0)
                if prev_pf < 0.8:
                    return {
                        "decision": "suspend",
                        "reason": (
                            f"Profit factor {profit_factor:.2f} below 0.8 for"
                            f" two consecutive evaluations"
                        ),
                        "trigger": "pf_below_08_2eval",
                    }
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

    # Determine whether the null distribution is valid for lifecycle decisions.
    # R12 safety latch (pre-run gate): never suspend/terminate for "not beating
    # null" when there is no benchmark data or the benchmark hasn't reached its
    # own significance floor (30 trades).  This prevents the meta-controller
    # from culling agents before a proper null distribution exists.
    null_valid = (
        null_metrics is not None
        and null_metrics.get("closed_trades", 0) >= 30
    )

    # Probation: borderline agent (p between 0.05-0.15 or PF 0.8-1.0)
    sig = significance_test(metrics, null_metrics, conn=conn)
    if total_trades >= 50:
        p_est = sig.get("p_value_estimate", ">0.10")
        beats = sig.get("beats_null", False)
        if not beats and p_est in ("<0.10", ">0.10"):
            if profit_factor < 1.0 and null_valid:
                return {
                    "decision": "suspend",
                    "reason": (
                        f"Not beating null (p={p_est}, PF={profit_factor:.2f})"
                        f" after {total_trades} trades"
                    ),
                    "trigger": "not_beating_null_50",
                }
        if total_trades >= 100 and not beats and null_valid:
            return {
                "decision": "terminate",
                "reason": (
                    f"Not beating null (p={p_est}, PF={profit_factor:.2f})"
                    f" after {total_trades} trades"
                ),
                "trigger": "not_beating_null_100",
            }

    # Check if suspended and due for restore-or-terminate
    if agent_status == "suspended":
        suspension = conn.execute(
            """SELECT evaluated_at, reason FROM evaluations
               WHERE agent_id = ? AND decision = 'suspend'
               ORDER BY id DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if suspension:
            try:
                suspended_at = datetime.fromisoformat(
                    suspension["evaluated_at"].replace("Z", "+00:00")
                )
                days_since_suspension = (
                    datetime.now(timezone.utc) - suspended_at
                ).days
                trades_since = conn.execute(
                    """SELECT COUNT(*) FROM trades
                       WHERE agent_id = ? AND status = 'closed' AND voided = 0
                       AND entry_timestamp > ?""",
                    (agent_id, suspension["evaluated_at"]),
                ).fetchone()[0]

                if days_since_suspension >= 7 or trades_since >= 10:
                    if profit_factor >= 0.8 and win_rate >= 0.40:
                        return {
                            "decision": "active",
                            "reason": (
                                f"Restored after suspension: PF={profit_factor:.2f},"
                                f" WR={win_rate:.1%}"
                            ),
                            "trigger": "restore_after_suspension",
                        }
                    else:
                        return {
                            "decision": "terminate",
                            "reason": (
                                f"Failed to improve after suspension:"
                                f" PF={profit_factor:.2f}, WR={win_rate:.1%}"
                            ),
                            "trigger": "failed_suspension_review",
                        }
            except (ValueError, TypeError):
                pass

    return {
        "decision": "active",
        "reason": f"All metrics within acceptable range (PF={profit_factor:.2f}, WR={win_rate:.1%})",
        "trigger": "none",
    }


def harvest_best_trades(
    conn, agent_id: str, count: int = 5,
) -> list[dict[str, Any]]:
    """Harvest the best (highest PnL%) closed trades from an agent.

    Inserts seed records into the seeds table.
    Returns the list of seed records inserted.
    """
    rows = conn.execute(
        """SELECT id, pnl_pct, agent_reasoning_json, hypothesis
           FROM trades
           WHERE agent_id = ? AND status = 'closed' AND voided = 0
             AND pnl_pct IS NOT NULL
           ORDER BY pnl_pct DESC
           LIMIT ?""",
        (agent_id, count),
    ).fetchall()

    seeds = []
    now = _now()
    for row in rows:
        conn.execute(
            """INSERT INTO seeds
                   (source_agent_id, trade_id, harvested_at, pnl_pct,
                    agent_reasoning_json, thesis_excerpt)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                row["id"],
                now,
                row["pnl_pct"],
                row["agent_reasoning_json"],
                (row["hypothesis"] or "")[:200] if row["hypothesis"] else None,
            ),
        )
        seeds.append(dict(row))

    conn.commit()
    return seeds
