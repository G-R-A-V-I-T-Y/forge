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
from dataclasses import dataclass, field
from pathlib import Path

from store.query import query_trades
from store.specs import get_active_spec

logger = logging.getLogger(__name__)

#: Maximum number of recent closed trades to include in the dossier.
MAX_CLOSED_TRADES = 50

#: Number of highest-regret decisions to surface.
TOP_REGRET_COUNT = 10

#: Feature columns extracted from trade records for feature-conditioned stats.
_FEATURE_COLUMNS = [
    "confidence",
    "funding_rate_current",
    "open_interest_24h_change_pct",
]

#: Training dataset path (relative to project root).
_TRAINING_DATASET = Path("data") / "historical_data" / "training_dataset.parquet"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dossier:
    """Evidence dossier consumed by the three-stage reflection pipeline.

    Every field is populated from best-effort data sources — missing files
    or empty tables yield empty lists/strings, never ``None`` or exceptions.
    """

    agent_id: str
    thesis_text: str
    active_spec_yaml: str
    closed_trades: list[dict]  # last 50 with entry-fingerprint summary
    calibration_curve: list[dict]
    top_regret_decisions: list[dict]  # top 10 highest-regret decisions
    regime_breakdown: list[dict]  # win-rate / PF by regime
    feature_stats: list[dict]  # per-feature bucketed fwd-return + win-rate
    hypothesis_history: list[dict]  # agent's own hypothesis track record
    desk_digest: str  # placeholder for M11 desk memory

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def to_prompt(self, max_chars: int = 8000) -> str:
        """Render the dossier as a formatted text block for the LLM prompt.

        Sections are emitted in priority order so that truncation (via
        :func:`truncate_prompt`) drops the least-critical tail first:

        1. Thesis text
        2. Active spec (YAML)
        3. Calibration curve
        4. Top regret decisions
        5. Regime breakdown
        6. Trade history (most recent last)
        7. Feature stats
        8. Hypothesis history
        9. Desk digest
        """
        sections: list[str] = []

        # 1. Thesis
        if self.thesis_text:
            sections.append(
                _section("THESIS", self.thesis_text)
            )

        # 2. Active spec
        if self.active_spec_yaml:
            sections.append(
                _section("ACTIVE SPEC (YAML)", self.active_spec_yaml)
            )

        # 3. Calibration
        if self.calibration_curve:
            lines = ["Bucket        Count   Win Rate"]
            for b in self.calibration_curve:
                lines.append(
                    f"  {b['bucket']:<13} {b['count']:>5}   "
                    f"{b['win_rate']:.1%}"
                )
            sections.append(_section("CALIBRATION CURVE", "\n".join(lines)))

        # 4. Top regret decisions
        if self.top_regret_decisions:
            lines = []
            for d in self.top_regret_decisions:
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
            sections.append(_section("TOP REGRET DECISIONS", "\n".join(lines)))

        # 5. Regime breakdown
        if self.regime_breakdown:
            lines = []
            for r in self.regime_breakdown:
                lines.append(
                    f"  {r['regime']:<16} WR={r['win_rate']:.1%}  "
                    f"PF={r.get('profit_factor', 0.0):.2f}  "
                    f"N={r['count']}"
                )
            sections.append(_section("REGIME BREAKDOWN", "\n".join(lines)))

        # 6. Trade history
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

        # 7. Feature stats
        if self.feature_stats:
            lines = []
            for fs in self.feature_stats:
                lines.append(
                    f"  {fs['feature']:<30} buckets={fs['n_buckets']}  "
                    f"best_bucket_wr={fs.get('best_bucket_wr', 0):.1%}  "
                    f"worst_bucket_wr={fs.get('worst_bucket_wr', 0):.1%}"
                )
            sections.append(_section("FEATURE STATS", "\n".join(lines)))

        # 8. Hypothesis history — this agent's own registry track record
        # (M10 crit 6): claim + resolution status + observed effect, so a
        # reflection cycle sees its own falsified ideas and cannot
        # re-propose them without addressing the falsification.
        if self.hypothesis_history:
            lines = []
            for h in self.hypothesis_history:
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

        # 9. Desk digest
        if self.desk_digest:
            sections.append(_section("DESK DIGEST", self.desk_digest))

        full_text = "\n\n".join(sections)
        return truncate_prompt(full_text, max_chars)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_dossier(
    conn,
    agent_id: str,
    ledger_dir: str | Path | None = None,
) -> Dossier:
    """Build an evidence dossier for *agent_id* from the live database.

    Parameters
    ----------
    conn:
        Open SQLite connection (read-only is fine).
    agent_id:
        Agent identifier.
    ledger_dir:
        Optional path to the ledger directory.  Currently unused by the
        dossier builder itself but reserved for future training-dataset
        reads in feature stats.

    Returns
    -------
    Dossier
        Fully populated (or gracefully empty) evidence dossier.
    """
    thesis_text = _read_thesis(agent_id)
    active_spec_yaml = _read_active_spec(conn, agent_id)
    closed_trades = _get_closed_trades(conn, agent_id)
    calibration_curve = _safe_calibration(conn, agent_id)
    top_regret_decisions = _get_top_regret_decisions(conn, agent_id)
    regime_breakdown = _compute_regime_breakdown(closed_trades)
    feature_stats = _compute_feature_stats(conn, agent_id, ledger_dir)
    hypothesis_history = _get_hypothesis_history(conn, agent_id)
    desk_digest = ""  # placeholder for M11 desk memory

    return Dossier(
        agent_id=agent_id,
        thesis_text=thesis_text,
        active_spec_yaml=active_spec_yaml,
        closed_trades=closed_trades,
        calibration_curve=calibration_curve,
        top_regret_decisions=top_regret_decisions,
        regime_breakdown=regime_breakdown,
        feature_stats=feature_stats,
        hypothesis_history=hypothesis_history,
        desk_digest=desk_digest,
    )


# ---------------------------------------------------------------------------
# Internal data sources — all best-effort, never raise
# ---------------------------------------------------------------------------

_THESES_DIR = Path(__file__).resolve().parent / "theses"


def _read_thesis(agent_id: str) -> str:
    """Read the agent's thesis markdown, scanning for the latest version."""
    if not _THESES_DIR.is_dir():
        return ""

    # Find the highest version file: {agent_id}_v{N}.md
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
    """Read the active spec and return its YAML text representation."""
    try:
        spec = get_active_spec(conn, agent_id)
    except Exception as exc:
        logger.warning("Failed to read active spec for %s: %s", agent_id, exc)
        return ""

    if spec is None:
        return ""

    # Reconstruct YAML from the specs table (already stored as yaml_text)
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

    # Fallback: reconstruct from the Spec object
    try:
        import yaml as _yaml  # noqa: PLC0415 – lazy to avoid top-level import

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
    """Fetch the last MAX_CLOSED_TRADES closed, non-voided trades.

    Returns lightweight dicts (decode_ohlcv=False) with an
    ``entry_fingerprint_summary`` field added.
    """
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
        # Build a compact fingerprint summary from the raw trade row
        summary = _build_fingerprint_summary(t)
        result.append({**t, "entry_fingerprint_summary": summary})
    return result


def _build_fingerprint_summary(trade: dict) -> dict:
    """Extract a compact summary from a trade's fingerprint fields."""
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


def _safe_calibration(conn, agent_id: str) -> list[dict]:
    """Compute calibration curve, swallowing all errors."""
    try:
        from agents.reflection import compute_calibration_curve as _ccc  # noqa: PLC0415
        return _ccc(conn, agent_id)
    except Exception as exc:
        logger.warning(
            "Calibration curve failed for %s: %s", agent_id, exc,
        )
        return []


def _get_top_regret_decisions(conn, agent_id: str) -> list[dict]:
    """Return the top-regret decisions for *agent_id*.

    Sources regret from ``decision_labels.regret_pct`` (real forward-
    simulated regret from ``meta/labeling.py``'s nightly job), joined back
    to ``decisions`` for the reason text and market context.

    Horizon policy: **max regret across horizons.**  A decision is ranked
    by whichever of its 1h/4h/24h ``regret_pct`` labels is largest, not
    just the 24h figure.  A decision can look fine at 1h and only reveal
    its regret once the 24h window plays out, or vice versa (a sharp 1h
    miss that mean-reverts by 24h) — taking the max surfaces every
    genuinely regrettable decision regardless of which horizon exposes it,
    and also means a decision that is too young to have a 24h label yet
    (but does have 1h/4h) still gets ranked instead of silently omitted.

    Falls back to the legacy ``counterfactual_was_better`` column ONLY
    when *this agent* has zero rows in ``decision_labels`` at all (e.g.
    before the nightly labeling job has ever run for it) — pre-labeling-
    era dossiers stay useful.  Once any label exists for the agent,
    labeled data always wins: the result is built exclusively from
    ``decision_labels``, even if that yields fewer than
    ``TOP_REGRET_COUNT`` rows, and legacy-flagged decisions with no label
    are never mixed back in.
    """
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
                continue  # keep only the max-regret horizon per decision
            seen.add(decision_id)
            result.append(_format_labeled_regret(row))
            if len(result) >= TOP_REGRET_COUNT:
                break
        return result

    # Fallback: no labels exist yet for this agent — use legacy columns.
    return _get_top_regret_decisions_legacy(conn, agent_id)


def _format_labeled_regret(row: dict) -> dict:
    """Format a ``decision_labels``-sourced row into a dossier regret entry."""
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
    """Best-effort asset/direction extraction from a decision's details blob.

    Mirrors ``meta/labeling.py``'s enter/wait extraction (order dict for
    ``enter``, candidate dict for ``wait``, both stored as a Python
    ``str(dict)`` repr inside the outer JSON) but only pulls the two
    fields the dossier prompt needs.  Returns ``{}`` on any parse failure
    or for actions (``close``) that don't carry this blob.
    """
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
    """Pre-labeling-era regret fallback, sourced from the ``decisions``
    table's ``counterfactual_was_better`` column.  Falls back to decisions
    with ``counterfactual_was_better = 1`` ranked by recency, then to
    ``decision_action = 'wait'`` decisions with counterfactual outcomes.
    """
    # Try counterfactual-based regret first
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

    # Fallback: all wait decisions with counterfactuals, ranked by regret
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
    """Format a decision row into a regret-scored dossier entry."""
    was_better = row.get("counterfactual_was_better", 0)
    # Regret score: 1.0 if counterfactual was better, decaying by rank
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


def _compute_regime_breakdown(trades: list[dict]) -> list[dict]:
    """Compute win-rate and profit factor by regime from closed trades."""
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

    result: list[dict] = []
    for regime in sorted(regimes):
        data = regimes[regime]
        total = data["total"]
        wins = data["wins"]
        wr = wins / total if total else 0.0
        pf = (
            min(data["win_pnl"] / data["loss_pnl"], 10.0)
            if data["loss_pnl"]
            else (10.0 if data["win_pnl"] else 0.0)
        )
        result.append({
            "regime": regime,
            "win_rate": round(wr, 4),
            "profit_factor": round(pf, 4),
            "count": total,
            "wins": wins,
            "losses": total - wins,
        })
    return result


def _compute_feature_stats(
    conn,
    agent_id: str,
    ledger_dir: str | Path | None = None,
) -> list[dict]:
    """Compute per-feature bucketed statistics.

    For each feature in ``_FEATURE_COLUMNS``, bucket the closed trades into
    quartiles by feature value and compute win-rate per bucket.

    Optionally reads from the training dataset parquet if available, but
    falls back to trade-level data which is always present.
    """
    # Try training dataset first for richer feature stats
    stats = _feature_stats_from_dataset(agent_id, ledger_dir)
    if stats:
        return stats

    # Fallback: trade-level feature stats
    return _feature_stats_from_trades(conn, agent_id)


def _feature_stats_from_dataset(
    agent_id: str,
    ledger_dir: str | Path | None = None,
) -> list[dict]:
    """Read from the training dataset parquet if available.

    Returns empty list if pyarrow/pandas not installed or file missing.
    """
    dataset_path = Path(_TRAINING_DATASET)
    if not dataset_path.exists():
        return []

    try:
        import pandas as _pd  # noqa: PLC0415 – optional dependency

        df = _pd.read_parquet(dataset_path)
    except Exception as exc:
        logger.debug(
            "Training dataset unavailable for feature stats: %s", exc,
        )
        return []

    stats: list[dict] = []
    for col in _FEATURE_COLUMNS:
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if series.empty:
            continue

        # Quartile buckets
        try:
            quantiles = series.quantile([0.25, 0.50, 0.75]).tolist()
        except Exception:
            continue

        buckets = _bucket_series(series, quantiles)
        best_wr = max((b["win_rate"] for b in buckets), default=0.0)
        worst_wr = min((b["win_rate"] for b in buckets), default=0.0)

        stats.append({
            "feature": col,
            "n_buckets": len(buckets),
            "n_samples": int(len(series)),
            "best_bucket_wr": round(best_wr, 4),
            "worst_bucket_wr": round(worst_wr, 4),
            "buckets": buckets,
        })

    return stats


def _bucket_series(series, quantiles: list[float]) -> list[dict]:
    """Bucket a pandas Series by quantile thresholds."""
    import pandas as _pd  # noqa: PLC0415

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


def _feature_stats_from_trades(conn, agent_id: str) -> list[dict]:
    """Compute feature stats directly from trade records."""
    try:
        trades = query_trades(
            conn,
            agent_id=agent_id,
            status="closed",
            decode_ohlcv=False,
            limit=200,
        )
    except Exception:
        return []

    closed = [t for t in trades if t.get("result") in ("win", "loss")]
    if not closed:
        return []

    stats: list[dict] = []
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

        best_wr = max((b["win_rate"] for b in buckets), default=0.0)
        worst_wr = min((b["win_rate"] for b in buckets), default=0.0)

        stats.append({
            "feature": col,
            "n_buckets": len(buckets),
            "n_samples": len(values),
            "best_bucket_wr": best_wr,
            "worst_bucket_wr": worst_wr,
            "buckets": buckets,
        })

    return stats


def _get_hypothesis_history(conn, agent_id: str) -> list[dict]:
    """Read the agent's own hypothesis REGISTRY track record (M10 crit 6),
    not the free-text ``trades.hypothesis`` column (a separate, unrelated
    per-trade note field).

    This is the evidence that lets the dossier show the LLM its own
    falsified ideas so a reflection cycle "cannot re-propose its own
    falsified ideas without addressing the falsification" -- the whole
    point of criterion 6. Sourced from
    ``agents.reflection.get_agent_hypothesis_history`` (agent_id,
    reflection_id, claim, feature, direction, regime_context,
    predicted_effect, falsification_condition, status, effect_observed,
    created_at, resolved_at), newest first, capped at 20 entries.

    Lazy-imported (matches ``_safe_calibration``'s pattern just above) to
    avoid a module-load cycle: agents/reflection.py imports
    ``build_dossier`` from this module at top level.
    """
    try:
        from agents.reflection import get_agent_hypothesis_history  # noqa: PLC0415

        rows = get_agent_hypothesis_history(conn, agent_id)
    except Exception as exc:
        logger.warning("Hypothesis registry history failed for %s: %s", agent_id, exc)
        return []

    return rows[:20]


# ---------------------------------------------------------------------------
# Prompt rendering helpers
# ---------------------------------------------------------------------------


def _section(title: str, body: str) -> str:
    """Format a titled section for the prompt."""
    return f"{title}\n{'-' * len(title)}\n{body}"


def _trunc(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* characters, appending ellipsis."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def truncate_prompt(text: str, max_chars: int) -> str:
    """Truncate *text* to at most *max_chars*, preserving structure.

    Strategy:
    1. If the full text fits, return as-is.
    2. Split into sections (double-newline separated).
    3. Emit sections in order until adding the next would exceed the limit.
    4. If a single section exceeds the limit, truncate at the last complete
       sentence within ``max_chars``, then append a continuation marker.

    Never truncates mid-record (mid-line in a table or list).
    """
    if len(text) <= max_chars:
        return text

    # Split into sections
    sections = text.split("\n\n")

    result_parts: list[str] = []
    remaining = max_chars

    for section in sections:
        # Account for the "\n\n" separator
        cost = len(section) + (2 if result_parts else 0)
        if cost <= remaining:
            result_parts.append(section)
            remaining -= cost
        else:
            # This section doesn't fit — try to fit a truncated version
            if remaining > 20:
                truncated = _truncate_at_sentence(section, remaining)
                if truncated.strip():
                    result_parts.append(truncated)
            break

    output = "\n\n".join(result_parts)

    # Append continuation marker if we dropped content
    if len(output) < len(text):
        marker = "\n\n[...truncated — full dossier exceeds char limit]"
        if len(output) + len(marker) <= max_chars:
            output += marker

    return output


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate *text* at the last complete sentence before *max_chars*.

    Falls back to word boundary if no sentence boundary is found.
    """
    if len(text) <= max_chars:
        return text

    # Try sentence boundaries
    truncated = text[:max_chars]
    last_period = truncated.rfind(".")
    last_newline = truncated.rfind("\n")

    # Use the latest sentence or line boundary
    boundary = max(last_period, last_newline)
    if boundary > max_chars * 0.5:
        return truncated[: boundary + 1]

    # Fall back to word boundary
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        return truncated[:last_space]

    return truncated
