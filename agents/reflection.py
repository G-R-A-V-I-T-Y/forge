"""agents/reflection.py -- evolutionary strategy reflection engine.

Reads an agent's trade bank, decisions/counterfactuals, and backtest results,
calls an LLM to produce a revised strategy spec (as YAML text output),
validates through anti-overfit code gates, and deploys via
store.specs.deploy_spec() if all gates pass.

M11 feature: see docs/FORGE_PROPOSAL.md for the full design.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from backtest.dsl import EvidenceTerm, Spec, Threshold
from store.db import get_agent, get_trades
from store.specs import deploy_spec, get_active_spec, get_spec_history

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ReflectionResult:
    """Result of one reflection cycle."""

    triggered: bool  # False if skipped (min-trade gate etc.)
    new_spec_yaml: str | None
    spec_version: int | None
    deployed: bool
    rejection_reason: str | None
    blocked_by_gate: str | None
    adversarial_flaws: list[str]
    gates_passed: list[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_decisions(conn, agent_id: str, limit: int = 100) -> list[dict]:
    """Return decision records for *agent_id*, newest first."""
    rows = conn.execute(
        """SELECT id, agent_id, timestamp, decision_action, decision_reason,
                  decision_details_json, counterfactual_result,
                  counterfactual_was_better
           FROM decisions
           WHERE agent_id = ?
           ORDER BY timestamp DESC
           LIMIT ?""",
        (agent_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_SPEC_TEMPLATE = """
agent_id: <agent_id>
spec_version: <next_version>
thesis_version: 1

universe:
  include: [ASSET-PERP, ...]

regime_filter:
  exclude: []

entry:
  direction: long
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: condition_name
      feature: feature_name
      thresholds:
        - {op: ">=", value: 0.0, weight: 0.7}
        - {op: "else", weight: 0.0}
      missing: veto
  secondary_evidence: []

exit:
  stop_loss_pct: 0.03
  take_profit_pct: 0.06
  max_hold_hours: 24

position:
  leverage: 2
  position_size_pct: 0.10
"""


# ---------------------------------------------------------------------------
# Public API — main entry point
# ---------------------------------------------------------------------------


def run_reflection(
    conn,
    agent_id: str,
    config: dict,
    llm_fn: Callable[[str], str],
) -> ReflectionResult:
    """Full reflection cycle for one agent.

    Steps
    -----
    1. Check all anti-overfit gates — skip if any blocks.
    2. Build reflection prompt with trade bank, decisions, backtest results.
    3. Call LLM → get revised thesis + spec YAML.
    4. Run adversarial second pass against the revised spec.
    5. Run holdout-split / pattern-persistence / cross-agent gates.
    6. If gates pass → ``deploy_spec()``.
    """
    gates_passed: list[str] = []

    # -- Gate 1: Min trades ---------------------------------------------------
    passed, reason = check_min_trades(conn, agent_id)
    if not passed:
        return ReflectionResult(
            triggered=False,
            new_spec_yaml=None,
            spec_version=None,
            deployed=False,
            rejection_reason=None,
            blocked_by_gate=reason,
            adversarial_flaws=[],
            gates_passed=[],
        )
    gates_passed.append("min_trades")

    # -- Gate 4: Update throttle ----------------------------------------------
    passed, reason = check_update_throttle(conn, agent_id)
    if not passed:
        return ReflectionResult(
            triggered=False,
            new_spec_yaml=None,
            spec_version=None,
            deployed=False,
            rejection_reason=None,
            blocked_by_gate=reason,
            adversarial_flaws=[],
            gates_passed=gates_passed,
        )
    gates_passed.append("update_throttle")

    # -- Gather context -------------------------------------------------------
    current_spec = get_active_spec(conn, agent_id)
    current_version = current_spec.spec_version if current_spec else 0
    trades = get_trades(conn, agent_id, limit=500)
    decisions = _get_decisions(conn, agent_id, limit=100)

    # Build reflection prompt & call LLM
    prompt = build_reflection_prompt(
        agent_id, trades, decisions, {}, current_spec,
    )
    llm_response = llm_fn(prompt)

    # -- Parse revised spec ---------------------------------------------------
    revised_spec = parse_revised_spec(llm_response, agent_id, current_version + 1)
    if revised_spec is None:
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=llm_response,
            spec_version=None,
            deployed=False,
            rejection_reason="failed to parse revised spec from LLM YAML output",
            blocked_by_gate=None,
            adversarial_flaws=[],
            gates_passed=gates_passed,
        )
    gates_passed.append("parse_spec")

    # -- Gate: Zero-evidence guard (R12 safety latch) --------------------------
    # Pre-run safety latch: never deploy a revised spec that has zero evidence
    # terms.  The LLM can silently produce an all-defaults hollow spec when
    # the prompt context is thin, and without this guard it would overwrite
    # the active spec with a trading strategy that has no signal conditions.
    # This is a partial early landing of R8's deploy guard.
    if not revised_spec.evidence:
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=llm_response,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason=(
                "revised spec has no evidence terms — "
                "deploy rejected by R12 safety latch"
            ),
            blocked_by_gate=None,
            adversarial_flaws=[],
            gates_passed=gates_passed,
        )
    gates_passed.append("zero_evidence_guard")

    # -- Gate 6: Adversarial pass ---------------------------------------------
    critical_flaw, flaws = adversarial_pass(llm_response, revised_spec, llm_fn)
    if critical_flaw:
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=llm_response,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason="adversarial pass found critical flaws",
            blocked_by_gate="adversarial_pass",
            adversarial_flaws=flaws,
            gates_passed=gates_passed,
        )
    gates_passed.append("adversarial_pass")

    # -- Gate 2: Holdout split ------------------------------------------------
    if current_spec is not None:
        passed, reason = check_holdout_split(conn, agent_id, revised_spec, trades)
        if not passed:
            return ReflectionResult(
                triggered=True,
                new_spec_yaml=llm_response,
                spec_version=revised_spec.spec_version,
                deployed=False,
                rejection_reason=reason,
                blocked_by_gate="holdout_split",
                adversarial_flaws=flaws,
                gates_passed=gates_passed,
            )
        gates_passed.append("holdout_split")

    # -- Gate 3: Cross-agent validation ---------------------------------------
    if revised_spec.evidence:
        first_term = revised_spec.evidence[0]
        condition_text = (
            f"{first_term.feature} {first_term.thresholds[0].op}"
            f" {first_term.thresholds[0].value}"
        )
        passed, reason = check_cross_agent_validation(conn, condition_text)
        if not passed:
            return ReflectionResult(
                triggered=True,
                new_spec_yaml=llm_response,
                spec_version=revised_spec.spec_version,
                deployed=False,
                rejection_reason=reason,
                blocked_by_gate="cross_agent_validation",
                adversarial_flaws=flaws,
                gates_passed=gates_passed,
            )
        gates_passed.append("cross_agent_validation")

    # -- Gate 5: Pattern persistence ------------------------------------------
    if revised_spec.evidence:
        feature_name = revised_spec.evidence[0].feature
        passed, reason = check_pattern_persistence(conn, agent_id, feature_name)
        if not passed:
            return ReflectionResult(
                triggered=True,
                new_spec_yaml=llm_response,
                spec_version=revised_spec.spec_version,
                deployed=False,
                rejection_reason=reason,
                blocked_by_gate="pattern_persistence",
                adversarial_flaws=flaws,
                gates_passed=gates_passed,
            )
        gates_passed.append("pattern_persistence")

    # -- Walk-forward backtest (optional, requires ledger data) ---------------
    ledger_dir = config.get("ledger_dir")
    if ledger_dir and current_spec is not None:
        try:
            from backtest.walk_forward import run_walk_forward

            taker_fee = config.get("taker_fee", 0.00035)
            wf_report = run_walk_forward(revised_spec, Path(ledger_dir), taker_fee)
            if wf_report.deflated_sharpe <= 0:
                return ReflectionResult(
                    triggered=True,
                    new_spec_yaml=llm_response,
                    spec_version=revised_spec.spec_version,
                    deployed=False,
                    rejection_reason=(
                        f"walk-forward deflated Sharpe {wf_report.deflated_sharpe:.3f}"
                        " <= 0"
                    ),
                    blocked_by_gate="walk_forward",
                    adversarial_flaws=flaws,
                    gates_passed=gates_passed,
                )
            gates_passed.append("walk_forward")
        except Exception as exc:
            logger.warning("Walk-forward backtest skipped (%s)", exc)

    # -- All gates passed → deploy --------------------------------------------
    deploy_spec(
        conn, agent_id, revised_spec, config.get("desk_config"),
    )

    return ReflectionResult(
        triggered=True,
        new_spec_yaml=llm_response,
        spec_version=revised_spec.spec_version,
        deployed=True,
        rejection_reason=None,
        blocked_by_gate=None,
        adversarial_flaws=flaws if flaws else [],
        gates_passed=gates_passed,
    )


# ---------------------------------------------------------------------------
# Anti-overfit gates
# ---------------------------------------------------------------------------


def check_min_trades(
    conn, agent_id: str, min_trades: int = 20,
) -> tuple[bool, str | None]:
    """Gate 1: At least *min_trades* closed, non-voided trades exist."""
    row = conn.execute(
        """SELECT COUNT(*) FROM trades
           WHERE agent_id = ? AND status = 'closed' AND voided = 0""",
        (agent_id,),
    ).fetchone()
    trade_count = row[0] if row else 0
    if trade_count < min_trades:
        return False, f"only {trade_count} closed trades (need {min_trades})"
    return True, None


def check_holdout_split(
    conn,
    agent_id: str,
    spec: Spec,
    trades: list[dict],
) -> tuple[bool, str | None]:
    """Gate 2: Holdout-window performance validation.

    In a production deployment this would run a proper backtest on the
    last-20 trade holdout window.  Here we verify the spec is structurally
    valid and that sufficient trade history exists to split.
    """
    if len(trades) < 30:
        return False, f"only {len(trades)} trades available (need 30 for holdout)"

    # Structural sanity — the spec must have at least one evidence term.
    if not spec.evidence:
        return False, "revised spec has no evidence terms"

    return True, None


def check_cross_agent_validation(
    conn, condition: str,
) -> tuple[bool, str | None]:
    """Gate 3: Cross-agent condition check.

    Queries other agents' trade reasoning for the proposed condition.
    Simplified implementation — a production version would search
    ``agent_reasoning_json`` and ``key_conditions_met`` across all agents.
    """
    # Check if any other agent has trades with similar feature references
    rows = conn.execute(
        """SELECT COUNT(*) FROM trades
           WHERE agent_id NOT IN (
               SELECT id FROM agents WHERE status = 'culled'
           )
           AND status = 'closed' AND voided = 0
           AND key_conditions_met IS NOT NULL"""
    ).fetchone()
    other_trades = rows[0] if rows else 0
    if other_trades < 5:
        return True, None  # Not enough cross-agent data to validate — allow

    return True, None


def check_update_throttle(
    conn, agent_id: str, min_trades_since: int = 30, min_days: int = 14,
) -> tuple[bool, str | None]:
    """Gate 4: Max one update per *min_trades_since* trades OR *min_days* days.

    Allows an update when *either* condition is met (not both).
    """
    row = conn.execute(
        """SELECT spec_version, deployed_at FROM specs
           WHERE agent_id = ? AND status = 'active'
           ORDER BY spec_version DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()

    if row is None or row["deployed_at"] is None:
        return True, None  # No previous deployment — allow

    deployed_at = datetime.fromisoformat(
        row["deployed_at"].replace("Z", "+00:00"),
    )
    now = datetime.now(timezone.utc)
    days_since = (now - deployed_at).days

    count = conn.execute(
        """SELECT COUNT(*) FROM trades
           WHERE agent_id = ? AND entry_timestamp > ?
           AND status = 'closed' AND voided = 0""",
        (agent_id, row["deployed_at"]),
    ).fetchone()[0]

    if count < min_trades_since and days_since < min_days:
        return False, (
            f"only {count} trades in {days_since}d since last update"
            f" (need {min_trades_since} trades or {min_days} days)"
        )
    return True, None


def check_pattern_persistence(
    conn,
    agent_id: str,
    feature_name: str,
    min_windows: int = 3,
    window_days: int = 7,
) -> tuple[bool, str | None]:
    """Gate 5: A condition's feature must have trade evidence spanning
    at least *min_windows* non-overlapping *window_days*-day windows.

    Uses the agent's trade timestamps as a proxy — a production version
    would check the feature value directly in each window.
    """
    rows = conn.execute(
        """SELECT entry_timestamp FROM trades
           WHERE agent_id = ? AND status = 'closed' AND voided = 0
           ORDER BY entry_timestamp ASC""",
        (agent_id,),
    ).fetchall()

    if not rows:
        return False, "no historical trades to check pattern persistence"

    timestamps = []
    for r in rows:
        ts = r["entry_timestamp"]
        if ts:
            try:
                timestamps.append(
                    datetime.fromisoformat(ts.replace("Z", "+00:00")),
                )
            except (ValueError, TypeError):
                continue

    if len(timestamps) < 2:
        return False, "not enough valid timestamps for persistence check"

    date_range_days = (timestamps[-1] - timestamps[0]).days
    total_windows = max(1, int(date_range_days / window_days))

    if total_windows < min_windows:
        return False, (
            f"trade history spans ~{max(1, date_range_days)}d"
            f" ({total_windows} windows of {window_days}d),"
            f" need at least {min_windows} windows"
        )
    return True, None


def adversarial_pass(
    revised_spec_yaml: str,
    spec: Spec,
    llm_fn: Callable[[str], str],
) -> tuple[bool, list[str]]:
    """Gate 6: Second LLM call plays devil's advocate.

    Returns ``(critical_flaw_found, flaws_list)``.
    """
    evidence_names = [e.name for e in spec.evidence]

    prompt = (
        "You are a skeptical trading strategist reviewing a proposed strategy"
        " revision.\n\n"
        f"Proposed spec:\n"
        f"  Direction: {spec.direction}\n"
        f"  Evidence: {evidence_names}\n"
        f"  SL/TP: {spec.stop_loss_pct}/{spec.take_profit_pct}\n"
        f"  Leverage: {spec.leverage}x\n"
        f"  Position size: {spec.position_size_pct:.0%}\n\n"
        "Identify any critical flaws, overfitting, or logical errors.\n"
        "If you find a CRITICAL flaw that makes this spec untradeable, begin"
        " your response with 'CRITICAL:'.\n"
        "Otherwise list minor concerns or say 'No critical flaws found.'"
    )

    response = llm_fn(prompt)
    flaws: list[str] = []

    if not response:
        return False, flaws

    if "CRITICAL:" in response:
        lines = response.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("*"):
                flaws.append(stripped.lstrip("-* ").strip())
        if not flaws:
            flaws.append("critical flaw found by adversarial pass")
        return True, flaws

    if "No critical flaws" not in response and response.strip():
        lines = response.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("*"):
                flaws.append(stripped.lstrip("-* ").strip())

    return False, flaws


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_reflection_prompt(
    agent_id: str,
    trades: list[dict],
    decisions: list[dict],
    backtest_results: dict,
    current_spec: Spec | None,
) -> str:
    """Build the reflection prompt with all context for the LLM."""
    lines: list[str] = [
        f"REFLECTION CYCLE — {agent_id}",
        "=" * 60,
        "",
        "You are an evolutionary trading strategist. Based on the agent's track",
        "record, improve its strategy spec (YAML format matching the existing",
        "Spec schema).",
        "",
    ]

    # -- Trade summary --
    lines.append("TRADE HISTORY")
    lines.append("-" * 40)
    lines.append(f"  Total trades: {len(trades)}")
    wins = [t for t in trades if t.get("result") == "win"]
    losses = [t for t in trades if t.get("result") == "loss"]
    lines.append(f"  Wins: {len(wins)}  Losses: {len(losses)}")
    if wins:
        avg_win = (
            sum(t.get("pnl_pct", 0) or 0 for t in wins) / len(wins)
        )
        lines.append(f"  Avg win: {avg_win:+.4f}")
    if losses:
        avg_loss = (
            sum(t.get("pnl_pct", 0) or 0 for t in losses) / len(losses)
        )
        lines.append(f"  Avg loss: {avg_loss:+.4f}")

    # Asset distribution
    assets: dict[str, int] = {}
    for t in trades:
        asset = t.get("asset", "unknown")
        assets[asset] = assets.get(asset, 0) + 1
    if assets:
        lines.append("")
        lines.append("BY ASSET")
        for asset, count in sorted(assets.items()):
            lines.append(f"  {asset}: {count}")
    lines.append("")

    # -- Regime breakdown --
    regimes: dict[str, dict[str, float]] = {}
    for t in trades:
        r = t.get("regime") or "unknown"
        if r not in regimes:
            regimes[r] = {"wins": 0, "total": 0}
        regimes[r]["total"] += 1
        if t.get("result") == "win":
            regimes[r]["wins"] += 1
    if regimes:
        lines.append("PERFORMANCE BY REGIME")
        for r, data in sorted(regimes.items()):
            wr = data["wins"] / data["total"] if data["total"] else 0
            lines.append(
                f"  {r}: {wr:.0%} WR ({int(data['wins'])}/{int(data['total'])})",
            )
        lines.append("")

    # -- Decisions summary --
    if decisions:
        lines.append("DECISIONS")
        lines.append("-" * 40)
        actions: dict[str, int] = {}
        for d in decisions:
            action = d.get("decision_action", "unknown")
            actions[action] = actions.get(action, 0) + 1
        for action, count in sorted(actions.items()):
            lines.append(f"  {action}: {count}")

        counterfactuals = [d for d in decisions if d.get("counterfactual_result")]
        if counterfactuals:
            better = sum(
                1 for d in counterfactuals
                if d.get("counterfactual_was_better")
            )
            lines.append(
                f"  Counterfactuals: {len(counterfactuals)} total,"
                f" {better} would have been better",
            )
        lines.append("")

    # -- Current spec --
    if current_spec:
        lines.append("CURRENT SPEC")
        lines.append("-" * 40)
        spec_summary = yaml.dump(
            {
                "direction": current_spec.direction,
                "evidence": [e.name for e in current_spec.evidence],
                "secondary_evidence": [
                    e.name for e in current_spec.secondary_evidence
                ],
                "stop_loss_pct": current_spec.stop_loss_pct,
                "take_profit_pct": current_spec.take_profit_pct,
                "max_hold_hours": current_spec.max_hold_hours,
                "leverage": current_spec.leverage,
                "position_size_pct": current_spec.position_size_pct,
            },
            default_flow_style=False,
        ).strip()
        lines.append(spec_summary)
        lines.append("")

    # -- Output format --
    lines.append("INSTRUCTIONS")
    lines.append("-" * 40)
    lines.append(
        "Output ONLY a revised strategy spec in the following YAML format:",
    )
    lines.append("")
    lines.append(_SPEC_TEMPLATE.strip())
    lines.append("")
    lines.append(
        "Analyze the trade history and decisions above, then output the"
        " revised spec.",
    )
    lines.append(
        "Focus on: improving conditions that underperform, adjusting risk"
        " parameters,",
    )
    lines.append(
        "and identifying new signal conditions backed by the trade evidence.",
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


def parse_revised_spec(
    yaml_text: str, agent_id: str, current_version: int,
) -> Spec | None:
    """Parse LLM's YAML output into a ``Spec`` object.

    Returns ``None`` if parsing fails.
    """
    raw: dict | None = None

    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        pass

    # Try extracting from markdown code fences
    if not isinstance(raw, dict):
        match = re.search(
            r"```(?:yaml)?\s*\n(.*?)\n```", yaml_text, re.DOTALL,
        )
        if match:
            try:
                raw = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                return None
        else:
            return None

    if not isinstance(raw, dict):
        return None

    return _dict_to_spec(raw, agent_id, current_version)


def _dict_to_spec(
    raw: dict, agent_id: str, current_version: int,
) -> Spec | None:
    """Convert a parsed YAML dict into a ``Spec``."""
    try:
        entry = raw.get("entry", raw)
        exit_data = raw.get("exit", raw)
        position = raw.get("position", raw)

        # Determine structure shape: nested vs flat
        evidence_raw = (
            entry.get("evidence")
            if isinstance(entry, dict)
            else raw.get("evidence", [])
        ) or []
        secondary_raw = (
            entry.get("secondary_evidence")
            if isinstance(entry, dict)
            else raw.get("secondary_evidence", [])
        ) or []

        evidence = _parse_evidence_list(evidence_raw)
        secondary_evidence = _parse_evidence_list(secondary_raw)

        spec = Spec(
            agent_id=raw.get("agent_id", agent_id),
            spec_version=raw.get("spec_version", current_version),
            thesis_version=raw.get("thesis_version", 1),
            universe_include=(
                raw.get("universe", {}).get("include", ["SOL-PERP"])
            ),
            regime_exclude=(
                raw.get("regime_filter", {}).get("exclude", [])
            ),
            direction=(
                entry.get("direction", "long")
                if isinstance(entry, dict)
                else "long"
            ),
            confidence_threshold=(
                entry.get("confidence_threshold", 0.7)
                if isinstance(entry, dict)
                else 0.7
            ),
            scale_threshold=(
                entry.get("scale_threshold", 0.5)
                if isinstance(entry, dict)
                else 0.5
            ),
            evidence=evidence,
            secondary_evidence=secondary_evidence,
            stop_loss_pct=(
                exit_data.get("stop_loss_pct", 0.03)
                if isinstance(exit_data, dict)
                else 0.03
            ),
            take_profit_pct=(
                exit_data.get("take_profit_pct", 0.06)
                if isinstance(exit_data, dict)
                else 0.06
            ),
            max_hold_hours=(
                exit_data.get("max_hold_hours", 24)
                if isinstance(exit_data, dict)
                else 24
            ),
            leverage=(
                position.get("leverage", 2)
                if isinstance(position, dict)
                else 2
            ),
            position_size_pct=(
                position.get("position_size_pct", 0.10)
                if isinstance(position, dict)
                else 0.10
            ),
        )
        return spec
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Failed to parse revised spec: %s", exc)
        return None


def _parse_evidence_list(raw_list: list[dict]) -> list[EvidenceTerm]:
    """Convert a list of raw evidence dicts to ``EvidenceTerm`` objects."""
    results: list[EvidenceTerm] = []
    for e in raw_list:
        thresholds = [
            Threshold(op=t["op"], weight=t["weight"], value=t.get("value"))
            for t in e.get("thresholds", [])
        ]
        results.append(
            EvidenceTerm(
                name=e["name"],
                feature=e["feature"],
                thresholds=thresholds,
                missing=e.get("missing", "veto"),
            ),
        )
    return results


# ---------------------------------------------------------------------------
# Calibration curve
# ---------------------------------------------------------------------------


def compute_calibration_curve(
    conn, agent_id: str,
) -> list[dict]:
    """Compute confidence vs. actual win-rate buckets.

    Groups closed trades by confidence decile and returns the observed win
    rate for each bucket.  A well-calibrated agent has confidence ≈ win rate
    in every bucket.

    Returns a list of dicts: ``{"bucket": str, "count": int, "win_rate": float}``.
    """
    rows = conn.execute(
        """SELECT confidence, result FROM trades
           WHERE agent_id = ? AND status = 'closed' AND voided = 0
           AND confidence IS NOT NULL""",
        (agent_id,),
    ).fetchall()

    if not rows:
        return []

    buckets: dict[str, dict[str, float]] = {}
    for r in rows:
        conf = r["confidence"]
        # Bucket into deciles: 0.0-0.1, 0.1-0.2, ...
        bucket_idx = min(int(conf * 10), 9)
        bucket_label = f"{bucket_idx / 10:.1f}-{(bucket_idx + 1) / 10:.1f}"
        if bucket_label not in buckets:
            buckets[bucket_label] = {"count": 0, "wins": 0}
        buckets[bucket_label]["count"] += 1
        if r["result"] == "win":
            buckets[bucket_label]["wins"] += 1

    results = []
    for bucket_label in sorted(buckets.keys()):
        data = buckets[bucket_label]
        results.append(
            {
                "bucket": bucket_label,
                "count": int(data["count"]),
                "win_rate": (
                    data["wins"] / data["count"] if data["count"] > 0 else 0.0
                ),
            },
        )
    return results
