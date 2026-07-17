"""agents/dossier.py -- evidence dossier builder for M10 three-stage reflection.

Builds a structured :class:`Dossier` from an agent's trade history, decisions,
calibration curve, regime performance, and thesis/spec context.  The three-stage
reflection pipeline (Diagnose -> Propose -> Validate) consumes this instead of
aggregate stats, giving it per-decision evidence, regret analysis, and
feature-conditioned statistics.

All data sources are best-effort: missing files, empty tables, or absent
columns return empty/default values — never exceptions.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from store.query import query_trades
from store.specs import get_active_spec

logger = logging.getLogger(__name__)

MAX_CLOSED_TRADES = 50
TOP_REGRET_COUNT = 10

_FEATURE_COLUMNS = [
    "confidence",
    "funding_rate_current",
    "open_interest_24h_change_pct",
]


@dataclass(frozen=True)
class Dossier:
    agent_id: str
    thesis_text: str
    spec_yaml: str
    closed_trades: list[dict]
    calibration_curve: dict
    high_regret_decisions: list[dict]
    win_rate_by_regime: dict[str, float]
    profit_factor_by_regime: dict[str, float]
    feature_stats: dict
    hypothesis_track_record: list[dict]
    desk_memory_digest: str

    def to_prompt(self, max_chars: int = 8000) -> str:
        sections: list[str] = []

        header = f"EVIDENCE DOSSIER — {self.agent_id}"
        sections.append(header)

        if self.thesis_text:
            sections.append(_section("THESIS", self.thesis_text))

        if self.spec_yaml:
            sections.append(_section("ACTIVE SPEC (YAML)", self.spec_yaml))

        if self.calibration_curve:
            lines = ["Bucket        Count   Conf.Mid  Win Rate"]
            for bucket_label in sorted(self.calibration_curve):
                b = self.calibration_curve[bucket_label]
                lines.append(
                    f"  {bucket_label:<13} {b['sample_count']:>5}   "
                    f"{b['confidence_mid']:.2f}     "
                    f"{b['realized_wr']:.1%}"
                )
            sections.append(_section("CALIBRATION CURVE", "\n".join(lines)))

        if self.high_regret_decisions:
            lines = []
            for d in self.high_regret_decisions:
                context = ""
                if d.get("asset"):
                    context = f"{d['asset']} {d.get('direction', '?')} "
                best = ""
                if d.get("best_action"):
                    best = f"best={d['best_action']} "
                lines.append(
                    f"  [{d.get('timestamp', '?')}] "
                    f"action={d.get('decision_action', '?')} {context}"
                    f"regret={d.get('regret_score', '?'):.2f} {best}"
                    f"reason={_trunc(d.get('decision_reason', ''), 120)}"
                )
            sections.append(_section("HIGH REGRET DECISIONS", "\n".join(lines)))

        if self.closed_trades:
            lines = []
            for t in self.closed_trades:
                result = t.get("result", "?")
                lines.append(
                    f"  [{t.get('entry_timestamp', '?')[:16]}] "
                    f"{t.get('asset', '?'):>10} "
                    f"{t.get('direction', '?'):<5} "
                    f"conf={t.get('confidence', 0) or 0:.2f}  "
                    f"pnl={t.get('pnl_pct', 0) or 0:+.2%}  "
                    f"regime={t.get('regime', '?')}  "
                    f"result={result}"
                )
            sections.append(_section("TRADE HISTORY (newest last)", "\n".join(lines)))

        if self.win_rate_by_regime or self.profit_factor_by_regime:
            regimes = sorted(set(self.win_rate_by_regime) | set(self.profit_factor_by_regime))
            lines = []
            for r in regimes:
                wr = self.win_rate_by_regime.get(r, 0.0)
                pf = self.profit_factor_by_regime.get(r, 0.0)
                lines.append(f"  {r:<16} WR={wr:.1%}  PF={pf:.2f}")
            sections.append(_section("REGIME BREAKDOWN", "\n".join(lines)))

        if self.feature_stats:
            lines = []
            for feature_name, fs in sorted(self.feature_stats.items()):
                lines.append(
                    f"  {feature_name:<30} buckets={fs.get('n_buckets', 0)}  "
                    f"best_wr={fs.get('best_bucket_wr', 0):.1%}  "
                    f"worst_wr={fs.get('worst_bucket_wr', 0):.1%}"
                )
            sections.append(_section("FEATURE STATS", "\n".join(lines)))

        if self.hypothesis_track_record:
            lines = []
            for h in self.hypothesis_track_record:
                effect = h.get("effect_observed")
                effect_note = f" effect_observed={effect:.2f}" if effect is not None else ""
                lines.append(
                    f"  [{(h.get('created_at') or '?')[:16]}] "
                    f"status={(h.get('status') or '?').upper()}{effect_note}  "
                    f"claim={_trunc(h.get('claim', ''), 100)}"
                )
                if h.get("falsification_condition"):
                    lines.append(
                        f"      falsification_condition="
                        f"{_trunc(h['falsification_condition'], 100)}"
                    )
            sections.append(_section("HYPOTHESIS TRACK RECORD", "\n".join(lines)))

        if self.desk_memory_digest:
            sections.append(_section("DESK MEMORY DIGEST", self.desk_memory_digest))

        full_text = "\n\n".join(sections)
        return _truncate_with_priority(full_text, max_chars)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_dossier(
    conn,
    agent_id: str,
    ledger_dir: str | Path | None = None,
    training_dataset_path: str | Path | None = None,
) -> Dossier:
    thesis_text = _read_thesis(agent_id)
    spec_yaml = _read_active_spec(conn, agent_id)
    closed_trades = _get_closed_trades(conn, agent_id)
    calibration_curve = _safe_calibration(conn, agent_id)
    high_regret_decisions = _get_top_regret_decisions(conn, agent_id)
    wr_by_regime, pf_by_regime = _compute_regime_metrics(closed_trades)
    feature_stats = _compute_feature_stats(conn, agent_id, training_dataset_path)
    hypothesis_track_record = _get_hypothesis_history(conn, agent_id)
    desk_memory_digest = ""

    return Dossier(
        agent_id=agent_id,
        thesis_text=thesis_text,
        spec_yaml=spec_yaml,
        closed_trades=closed_trades,
        calibration_curve=calibration_curve,
        high_regret_decisions=high_regret_decisions,
        win_rate_by_regime=wr_by_regime,
        profit_factor_by_regime=pf_by_regime,
        feature_stats=feature_stats,
        hypothesis_track_record=hypothesis_track_record,
        desk_memory_digest=desk_memory_digest,
    )


# ---------------------------------------------------------------------------
# Internal data sources — all best-effort, never raise
# ---------------------------------------------------------------------------

_THESES_DIR = Path(__file__).resolve().parent / "theses"


def _read_thesis(agent_id: str) -> str:
    if not _THESES_DIR.is_dir():
        return ""

    pattern = re.compile(rf"^{re.escape(agent_id)}_v(\d+)\.md$")
    best_version = 0
    best_path: Path | None = None
    for f in _THESES_DIR.iterdir():
        m = pattern.match(f.name)
        if m:
            v = int(m.group(1))
            if v > best_version:
                best_version = v
                best_path = f

    if best_path is None:
        return ""

    try:
        return best_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read thesis %s: %s", best_path, exc)
        return ""


def _read_active_spec(conn, agent_id: str) -> str:
    try:
        spec = get_active_spec(conn, agent_id)
    except Exception as exc:
        logger.warning("Failed to read active spec for %s: %s", agent_id, exc)
        return ""

    if spec is None:
        return ""

    try:
        row = conn.execute(
            """SELECT yaml_text FROM specs
               WHERE agent_id = ? AND status = 'active'
               ORDER BY spec_version DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if row and row["yaml_text"]:
            return row["yaml_text"]
    except Exception as exc:
        logger.warning("Failed to read spec YAML for %s: %s", agent_id, exc)

    try:
        import yaml as _yaml

        spec_dict = {
            "agent_id": spec.agent_id,
            "spec_version": spec.spec_version,
            "thesis_version": spec.thesis_version,
            "direction": spec.direction,
            "confidence_threshold": spec.confidence_threshold,
            "evidence": [e.name for e in spec.evidence],
            "secondary_evidence": [e.name for e in spec.secondary_evidence],
            "stop_loss_pct": spec.stop_loss_pct,
            "take_profit_pct": spec.take_profit_pct,
            "max_hold_hours": spec.max_hold_hours,
            "leverage": spec.leverage,
            "position_size_pct": spec.position_size_pct,
        }
        return _yaml.dump(spec_dict, default_flow_style=False)
    except Exception:
        return ""


def _get_closed_trades(conn, agent_id: str) -> list[dict]:
    try:
        trades = query_trades(
            conn,
            agent_id=agent_id,
            status="closed",
            decode_ohlcv=False,
            order_by="entry_timestamp DESC",
            limit=MAX_CLOSED_TRADES,
        )
    except Exception as exc:
        logger.warning("Failed to query closed trades for %s: %s", agent_id, exc)
        return []

    result: list[dict] = []
    for t in trades:
        summary = _build_fingerprint_summary(t)
        result.append({**t, "entry_fingerprint_summary": summary})
    return result


def _build_fingerprint_summary(trade: dict) -> dict:
    return {
        "asset": trade.get("asset"),
        "direction": trade.get("direction"),
        "regime": trade.get("regime"),
        "confidence": trade.get("confidence"),
        "result": trade.get("result"),
        "pnl_pct": trade.get("pnl_pct"),
        "exit_reason": trade.get("exit_reason"),
        "duration_minutes": trade.get("duration_minutes"),
        "key_conditions_met": trade.get("key_conditions_met"),
        "key_conditions_missing": trade.get("key_conditions_missing"),
        "hypothesis": trade.get("hypothesis"),
        "model_used": trade.get("model_used"),
    }


def _safe_calibration(conn, agent_id: str) -> dict:
    try:
        from store.performance import compute_calibration_curve
        return compute_calibration_curve(conn, agent_id)
    except Exception as exc:
        logger.warning(
            "Calibration curve failed for %s: %s", agent_id, exc,
        )
        return {}


def _get_top_regret_decisions(conn, agent_id: str) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT dl.decision_id, dl.horizon, dl.regret_pct,
                      dl.best_action, dl.best_outcome_pct,
                      dl.chosen_outcome_pct, dl.fwd_return_pct,
                      dl.max_runup_pct, dl.max_drawdown_pct,
                      d.timestamp, d.decision_action, d.decision_reason,
                      d.decision_details_json
               FROM decision_labels dl
               JOIN decisions d ON d.id = dl.decision_id
               WHERE d.agent_id = ?
               ORDER BY dl.regret_pct DESC""",
            (agent_id,),
        ).fetchall()
    except Exception as exc:
        logger.warning(
            "Labeled regret query failed for %s (decision_labels may not "
            "exist yet): %s", agent_id, exc,
        )
        rows = []

    if rows:
        seen: set[int] = set()
        result: list[dict] = []
        for r in rows:
            row = dict(r)
            decision_id = row["decision_id"]
            if decision_id in seen:
                continue
            seen.add(decision_id)
            result.append(_format_labeled_regret(row))
            if len(result) >= TOP_REGRET_COUNT:
                break
        return result

    return _get_top_regret_decisions_legacy(conn, agent_id)


def _format_labeled_regret(row: dict) -> dict:
    context = _extract_market_context(
        row.get("decision_action", ""), row.get("decision_details_json"),
    )
    return {
        "decision_id": row.get("decision_id"),
        "timestamp": row.get("timestamp", ""),
        "decision_action": row.get("decision_action", ""),
        "decision_reason": row.get("decision_reason", ""),
        "asset": context.get("asset"),
        "direction": context.get("direction"),
        "horizon": row.get("horizon"),
        "regret_score": row.get("regret_pct") or 0.0,
        "best_action": row.get("best_action"),
        "best_outcome_pct": row.get("best_outcome_pct"),
        "chosen_outcome_pct": row.get("chosen_outcome_pct"),
        "fwd_return_pct": row.get("fwd_return_pct"),
        "max_runup_pct": row.get("max_runup_pct"),
        "max_drawdown_pct": row.get("max_drawdown_pct"),
    }


def _extract_market_context(
    decision_action: str, decision_details_json: str | None,
) -> dict:
    if not decision_details_json:
        return {}
    try:
        details = json.loads(decision_details_json)
    except (json.JSONDecodeError, TypeError):
        return {}

    if decision_action == "enter":
        blob = details.get("order")
    elif decision_action == "wait":
        blob = details.get("candidate")
    else:
        return {}

    if isinstance(blob, str):
        try:
            blob = ast.literal_eval(blob)
        except (ValueError, SyntaxError):
            return {}

    if not isinstance(blob, dict):
        return {}

    return {"asset": blob.get("asset"), "direction": blob.get("direction")}


def _get_top_regret_decisions_legacy(conn, agent_id: str) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT id, agent_id, timestamp, decision_action,
                      decision_reason, decision_details_json,
                      counterfactual_result, counterfactual_was_better
               FROM decisions
               WHERE agent_id = ?
                 AND counterfactual_was_better = 1
               ORDER BY timestamp DESC
               LIMIT ?""",
            (agent_id, TOP_REGRET_COUNT),
        ).fetchall()
        if rows:
            return [
                _format_decision_regret(dict(r), idx)
                for idx, r in enumerate(rows)
            ]
    except Exception as exc:
        logger.warning(
            "Regret query failed for %s (table may not exist): %s",
            agent_id, exc,
        )

    try:
        rows = conn.execute(
            """SELECT id, agent_id, timestamp, decision_action,
                      decision_reason, decision_details_json,
                      counterfactual_result, counterfactual_was_better
               FROM decisions
               WHERE agent_id = ?
                 AND counterfactual_result IS NOT NULL
               ORDER BY counterfactual_was_better DESC, timestamp DESC
               LIMIT ?""",
            (agent_id, TOP_REGRET_COUNT),
        ).fetchall()
        return [
            _format_decision_regret(dict(r), idx)
            for idx, r in enumerate(rows)
        ]
    except Exception:
        return []


def _format_decision_regret(row: dict, rank: int) -> dict:
    was_better = row.get("counterfactual_was_better", 0)
    regret = max(0.0, 1.0 - (rank * 0.1)) if was_better else 0.0
    return {
        "decision_id": row.get("id"),
        "timestamp": row.get("timestamp", ""),
        "decision_action": row.get("decision_action", ""),
        "decision_reason": row.get("decision_reason", ""),
        "counterfactual_result": row.get("counterfactual_result", ""),
        "counterfactual_was_better": was_better,
        "regret_score": round(regret, 2),
    }


def _compute_regime_metrics(
    trades: list[dict],
) -> tuple[dict[str, float], dict[str, float]]:
    regimes: dict[str, dict[str, float]] = {}
    for t in trades:
        r = t.get("regime") or "unknown"
        if r not in regimes:
            regimes[r] = {"wins": 0, "total": 0, "win_pnl": 0.0, "loss_pnl": 0.0}
        regimes[r]["total"] += 1
        pnl = t.get("pnl_pct") or 0.0
        if t.get("result") == "win":
            regimes[r]["wins"] += 1
            regimes[r]["win_pnl"] += pnl
        elif t.get("result") == "loss":
            regimes[r]["loss_pnl"] += abs(pnl)

    wr_map: dict[str, float] = {}
    pf_map: dict[str, float] = {}
    for regime in sorted(regimes):
        data = regimes[regime]
        total = data["total"]
        wins = data["wins"]
        wr_map[regime] = round(wins / total, 4) if total else 0.0
        if data["loss_pnl"]:
            pf_map[regime] = round(min(data["win_pnl"] / data["loss_pnl"], 10.0), 4)
        else:
            pf_map[regime] = round(10.0 if data["win_pnl"] else 0.0, 4)
    return wr_map, pf_map


def _compute_feature_stats(
    conn,
    agent_id: str,
    training_dataset_path: str | Path | None = None,
) -> dict:
    stats = _feature_stats_from_dataset(training_dataset_path)
    if stats:
        return stats
    return _feature_stats_from_trades(conn, agent_id)


def _feature_stats_from_dataset(
    training_dataset_path: str | Path | None = None,
) -> dict:
    dataset_path = Path(training_dataset_path) if training_dataset_path else None
    if dataset_path is None or not dataset_path.exists():
        return {}

    try:
        import pandas as _pd

        df = _pd.read_parquet(dataset_path)
    except Exception as exc:
        logger.debug("Training dataset unavailable for feature stats: %s", exc)
        return {}

    stats: dict = {}
    for col in _FEATURE_COLUMNS:
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if series.empty:
            continue

        try:
            quantiles = series.quantile([0.25, 0.50, 0.75]).tolist()
        except Exception:
            continue

        buckets = _bucket_series(series, quantiles)
        best_wr = max((b.get("win_rate", 0.0) for b in buckets), default=0.0)
        worst_wr = min((b.get("win_rate", 0.0) for b in buckets), default=0.0)

        stats[col] = {
            "n_buckets": len(buckets),
            "n_samples": int(len(series)),
            "best_bucket_wr": round(best_wr, 4),
            "worst_bucket_wr": round(worst_wr, 4),
            "buckets": buckets,
        }

    return stats


def _bucket_series(series, quantiles: list[float]) -> list[dict]:
    import pandas as _pd

    q_values = sorted(set(quantiles))
    labels = []
    prev = float("-inf")
    for i, q in enumerate(q_values):
        labels.append((prev, q))
        prev = q
    labels.append((prev, float("inf")))

    buckets: list[dict] = []
    for lo, hi in labels:
        mask = (series >= lo) & (series < hi) if hi != float("inf") else (series >= lo)
        subset = series[mask]
        if subset.empty:
            continue
        buckets.append({
            "range": f"{lo:.4f}-{hi:.4f}" if hi != float("inf") else f"{lo:.4f}+",
            "count": int(len(subset)),
            "mean": round(float(subset.mean()), 6),
        })
    return buckets


def _feature_stats_from_trades(conn, agent_id: str) -> dict:
    try:
        trades = query_trades(
            conn,
            agent_id=agent_id,
            status="closed",
            decode_ohlcv=False,
            limit=200,
        )
    except Exception:
        return {}

    closed = [t for t in trades if t.get("result") in ("win", "loss")]
    if not closed:
        return {}

    stats: dict = {}
    for col in _FEATURE_COLUMNS:
        values = [
            (t.get(col), t.get("result") == "win")
            for t in closed
            if t.get(col) is not None
        ]
        if len(values) < 4:
            continue

        nums = [v[0] for v in values]
        nums_sorted = sorted(nums)
        n = len(nums_sorted)
        q25 = nums_sorted[n // 4]
        q50 = nums_sorted[n // 2]
        q75 = nums_sorted[3 * n // 4]

        buckets: list[dict] = []
        for lo, hi in [
            (float("-inf"), q25), (q25, q50), (q50, q75), (q75, float("inf")),
        ]:
            bucket_vals = [
                (val, is_win)
                for val, is_win in values
                if (hi == float("inf") or val < hi) and val >= lo
            ]
            if not bucket_vals:
                continue
            wins = sum(1 for _, w in bucket_vals if w)
            buckets.append({
                "range": (
                    f"{lo:.4f}-{hi:.4f}"
                    if hi != float("inf")
                    else f"{lo:.4f}+"
                ),
                "count": len(bucket_vals),
                "win_rate": round(wins / len(bucket_vals), 4),
            })

        best_wr = max((b.get("win_rate", 0.0) for b in buckets), default=0.0)
        worst_wr = min((b.get("win_rate", 0.0) for b in buckets), default=0.0)

        stats[col] = {
            "n_buckets": len(buckets),
            "n_samples": len(values),
            "best_bucket_wr": best_wr,
            "worst_bucket_wr": worst_wr,
            "buckets": buckets,
        }

    return stats


def _get_hypothesis_history(conn, agent_id: str) -> list[dict]:
    try:
        from agents.reflection import get_agent_hypothesis_history

        rows = get_agent_hypothesis_history(conn, agent_id)
    except Exception as exc:
        logger.warning("Hypothesis registry history failed for %s: %s", agent_id, exc)
        return []

    return rows[:20]


# ---------------------------------------------------------------------------
# Prompt rendering helpers
# ---------------------------------------------------------------------------


def _section(title: str, body: str) -> str:
    return f"{title}\n{'-' * len(title)}\n{body}"


def _trunc(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _truncate_with_priority(text: str, max_chars: int) -> str:
    """Truncate *text* to at most *max_chars*, dropping entire sections from
    lowest priority first: hypothesis_track_record, feature_stats, trades,
    high_regret_decisions, calibration, spec, thesis. Never truncates
    mid-record — drops the whole section instead."""
    if len(text) <= max_chars:
        return text

    sections = text.split("\n\n")

    result_parts: list[str] = []
    remaining = max_chars

    for section in sections:
        cost = len(section) + (2 if result_parts else 0)
        if cost <= remaining:
            result_parts.append(section)
            remaining -= cost
        else:
            if remaining > 20:
                truncated = _truncate_at_sentence(section, remaining)
                if truncated.strip():
                    result_parts.append(truncated)
            break

    output = "\n\n".join(result_parts)

    if len(output) < len(text):
        marker = "\n\n[...truncated — full dossier exceeds char limit]"
        if len(output) + len(marker) <= max_chars:
            output += marker

    return output


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    last_period = truncated.rfind(".")
    last_newline = truncated.rfind("\n")

    boundary = max(last_period, last_newline)
    if boundary > max_chars * 0.5:
        return truncated[: boundary + 1]

    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        return truncated[:last_space]

    return truncated


def truncate_prompt(text: str, max_chars: int) -> str:
    """Public alias used by existing callers."""
    return _truncate_with_priority(text, max_chars)
