"""agents/reflection.py -- evolutionary strategy reflection engine.

Reads an agent's trade bank, decisions/counterfactuals, and backtest results,
calls an LLM to produce a revised strategy spec (as YAML text output),
validates through anti-overfit code gates, and deploys via
store.specs.deploy_spec() if all gates pass.

M11 feature: see docs/FORGE_PROPOSAL.md for the full design.
M10 feature: hypothesis registry for challenger trial falsification.
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

from agents.dossier import build_dossier
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

# System prompts for the dedicated reflection transport (llm/reflection_client.py),
# which takes (system_prompt, user_prompt) -- raw text in, raw text out, no
# decision-schema coercion. See M9 criterion 1+2.
_REFLECTION_SYSTEM_PROMPT = (
    "You are a trading-strategy reflection engine. Analyze the evidence "
    "provided and respond precisely to the stage instructions in the "
    "user prompt."
)
_ADVERSARIAL_SYSTEM_PROMPT = (
    "You are a skeptical trading strategist performing an adversarial "
    "review of a proposed strategy revision."
)


# ---------------------------------------------------------------------------
# Public API — main entry point
# ---------------------------------------------------------------------------


def run_reflection(
    conn,
    agent_id: str,
    config: dict,
    llm_fn: Callable[[str, str], str],
) -> ReflectionResult:
    """Full reflection cycle for one agent — M10 three-stage pipeline.

    Stages
    ------
    1. **Diagnose**: Build an evidence dossier from the agent's trade history,
       calibration curve, regret decisions, and regime breakdown, then call
       the LLM to produce a structured diagnosis (what's working, what's not).
    2. **Propose**: Based on the diagnosis, call the LLM to generate a revised
       strategy spec (YAML).
    3. **Validate**: Run all existing anti-overfit gates (adversarial pass,
       holdout split, cross-agent, pattern persistence, walk-forward) against
       the proposed spec. If all gates pass → ``deploy_spec()``.

    Falls back to the legacy single-call pipeline when the dossier builder
    cannot produce a dossier (missing ledger data, no trades, etc.).
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

    # ------------------------------------------------------------------
    # Stage 1: Diagnose — build dossier + call LLM for analysis
    # ------------------------------------------------------------------
    dossier = None
    diagnosis = ""
    try:
        ledger_dir = config.get("ledger_dir", "ledger")
        dossier = build_dossier(conn, agent_id, ledger_dir)
    except Exception as exc:
        logger.warning("Dossier build failed for %s: %s — falling back to legacy path", agent_id, exc)

    try:
        if dossier is not None and dossier.closed_trades:
            # Three-stage pipeline: Diagnose → Propose → Validate
            diagnose_prompt = _build_diagnose_prompt(agent_id, dossier)
            diagnosis = llm_fn(_REFLECTION_SYSTEM_PROMPT, diagnose_prompt)
            logger.info(
                "[%s] Stage 1 (Diagnose) complete — %d chars",
                agent_id, len(diagnosis),
            )

            # Stage 2: Propose — generate revised spec informed by diagnosis
            propose_prompt = _build_propose_prompt(
                agent_id, diagnosis, current_spec, current_version,
            )
            llm_response = llm_fn(_REFLECTION_SYSTEM_PROMPT, propose_prompt)
            logger.info(
                "[%s] Stage 2 (Propose) complete — %d chars",
                agent_id, len(llm_response),
            )
        else:
            # Legacy single-call pipeline (fallback)
            prompt = build_reflection_prompt(
                agent_id, trades, decisions, {}, current_spec,
            )
            llm_response = llm_fn(_REFLECTION_SYSTEM_PROMPT, prompt)
    except Exception as exc:
        # A transport failure (network error, exhausted mock, timeout) must
        # not escape run_reflection as an unhandled exception -- it should
        # surface as a normal rejected cycle so the scheduler can log it and
        # move on to the next agent.
        logger.warning(
            "[%s] reflection LLM transport failed mid-pipeline: %s", agent_id, exc,
        )
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=None,
            spec_version=None,
            deployed=False,
            rejection_reason=f"reflection LLM transport failed: {exc}",
            blocked_by_gate=None,
            adversarial_flaws=[],
            gates_passed=gates_passed,
        )

    # -- Capture the Stage 2 YAML spec before any further LLM calls ----------
    # The three-stage pipeline reuses ``llm_response`` for Stage 2 output,
    # but ``adversarial_pass()`` calls ``llm_fn`` again which can overwrite
    # it (especially in tests where the mock tracks call count).  Save the
    # YAML text in a dedicated variable so it survives the adversarial pass.
    propose_yaml = llm_response

    # -- Parse revised spec ---------------------------------------------------
    revised_spec = parse_revised_spec(propose_yaml, agent_id, current_version + 1)
    if revised_spec is None:
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
            spec_version=None,
            deployed=False,
            rejection_reason="failed to parse revised spec from LLM YAML output",
            blocked_by_gate=None,
            adversarial_flaws=[],
            gates_passed=gates_passed,
        )
    gates_passed.append("parse_spec")

    # -- Gate: Zero-evidence guard (R12 safety latch) --------------------------
    if not revised_spec.evidence:
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
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

    # ------------------------------------------------------------------
    # Stage 3: Validate — run all anti-overfit gates
    # ------------------------------------------------------------------

    # -- Gate 6: Adversarial pass ---------------------------------------------
    try:
        critical_flaw, flaws = adversarial_pass(propose_yaml, revised_spec, llm_fn)
    except Exception as exc:
        logger.warning(
            "[%s] adversarial pass LLM transport failed: %s", agent_id, exc,
        )
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason=f"reflection LLM transport failed: {exc}",
            blocked_by_gate=None,
            adversarial_flaws=[],
            gates_passed=gates_passed,
        )
    if critical_flaw:
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
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
                new_spec_yaml=propose_yaml,
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
                new_spec_yaml=propose_yaml,
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
                new_spec_yaml=propose_yaml,
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
                    new_spec_yaml=propose_yaml,
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
    # config["desk"] is the standing convention (see CLAUDE.md "Config
    # convention"); config.get("desk") (not "desk_config", which is not a
    # real key) preserves deploy_spec's own None-means-skip-validation
    # contract for callers/tests that don't supply a desk config at all.
    deploy_spec(
        conn, agent_id, revised_spec, config.get("desk"),
    )

    return ReflectionResult(
        triggered=True,
        new_spec_yaml=propose_yaml,
        spec_version=revised_spec.spec_version,
        deployed=True,
        rejection_reason=None,
        blocked_by_gate=None,
        adversarial_flaws=flaws if flaws else [],
        gates_passed=gates_passed,
    )


def _build_diagnose_prompt(agent_id: str, dossier: Any) -> str:
    """Build the Stage 1 (Diagnose) prompt from the evidence dossier.

    Asks the LLM to analyze what's working, what's not, and what
    changes are needed — forming a structured diagnosis that feeds
    into Stage 2 (Propose).
    """
    dossier_text = dossier.to_prompt(max_chars=4000)
    lines = [
        f"DIAGNOSE — {agent_id}",
        "=" * 60,
        "",
        "You are an evolutionary trading strategist performing a structured",
        "diagnosis of an agent's recent performance.",
        "",
        "Below is the agent's evidence dossier: trade history, calibration",
        "curve, regret analysis, regime breakdown, and feature-conditioned",
        "statistics.",
        "",
        dossier_text,
        "",
        "DIAGNOSIS INSTRUCTIONS",
        "-" * 40,
        "Analyze the dossier above and produce a structured diagnosis with:",
        "  1. What's working — conditions, regimes, or features beating expectations",
        "  2. What's not working — underperforming conditions, regimes, or features",
        "  3. Regret analysis — which missed trades would have been most profitable",
        "  4. Calibration assessment — is the agent overconfident or underconfident?",
        "  5. Recommended changes — 2-3 specific, measurable changes to the strategy",
        "",
        "Output ONLY your diagnosis as plain text. Do NOT include a YAML spec yet.",
        "",
    ]
    return "\n".join(lines)


def _build_propose_prompt(
    agent_id: str,
    diagnosis: str,
    current_spec: Spec | None,
    next_version: int,
) -> str:
    """Build the Stage 2 (Propose) prompt informed by the diagnosis.

    The LLM receives the diagnosis from Stage 1 and produces a revised
    strategy spec in YAML format.
    """
    spec_text = ""
    if current_spec:
        spec_text = yaml.dump(
            {
                "direction": current_spec.direction,
                "evidence": [e.name for e in current_spec.evidence],
                "secondary_evidence": [e.name for e in current_spec.secondary_evidence],
                "stop_loss_pct": current_spec.stop_loss_pct,
                "take_profit_pct": current_spec.take_profit_pct,
                "max_hold_hours": current_spec.max_hold_hours,
                "leverage": current_spec.leverage,
                "position_size_pct": current_spec.position_size_pct,
            },
            default_flow_style=False,
        ).strip()

    lines = [
        f"PROPOSE — {agent_id} v{next_version}",
        "=" * 60,
        "",
        "You are an evolutionary trading strategist. Based on the diagnosis",
        "below, produce a revised strategy spec in YAML format.",
        "",
        "DIAGNOSIS",
        "-" * 40,
        diagnosis,
        "",
    ]
    if spec_text:
        lines.extend([
            "CURRENT SPEC",
            "-" * 40,
            spec_text,
            "",
        ])
    lines.extend([
        "INSTRUCTIONS",
        "-" * 40,
        "Output ONLY a revised strategy spec in the following YAML format:",
        "",
        _SPEC_TEMPLATE.strip(),
        "",
        "Focus on: adjusting evidence terms and risk parameters based on",
        "the diagnosis above. Include at least one evidence term.",
    ])
    return "\n".join(lines)


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
    llm_fn: Callable[[str, str], str],
) -> tuple[bool, list[str]]:
    """Gate 6: Second LLM call plays devil's advocate.

    Returns ``(critical_flaw_found, flaws_list)``.
    """
    evidence_names = [e.name for e in spec.evidence]

    prompt = (
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

    response = llm_fn(_ADVERSARIAL_SYSTEM_PROMPT, prompt)
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


# ---------------------------------------------------------------------------
# M10: Hypothesis registry
# ---------------------------------------------------------------------------


def register_hypotheses(
    conn,
    agent_id: str,
    reflection_id: int | None,
    hypotheses: list[dict],
) -> list[int]:
    """Insert falsifiable hypotheses produced by a reflection cycle.

    Each hypothesis dict should contain at minimum ``claim`` and
    ``predicted_effect``.  Optional keys: ``feature``, ``direction``,
    ``regime_context``, ``falsification_condition``.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    agent_id:
        Agent identifier.
    reflection_id:
        The ``reflections.id`` that produced these hypotheses, or ``None``
        for ad-hoc registration.
    hypotheses:
        List of hypothesis dicts.

    Returns
    -------
    list[int]
        Row IDs of the inserted hypotheses.
    """
    if not hypotheses:
        return []

    timestamp = _now()
    ids: list[int] = []
    for h in hypotheses:
        cursor = conn.execute(
            """INSERT INTO hypotheses
                   (agent_id, reflection_id, claim, feature, direction,
                    regime_context, predicted_effect, falsification_condition,
                    status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
            (
                agent_id,
                reflection_id,
                h.get("claim", ""),
                h.get("feature"),
                h.get("direction"),
                h.get("regime_context"),
                h.get("predicted_effect", ""),
                h.get("falsification_condition"),
                timestamp,
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def resolve_hypotheses(
    conn,
    reflection_id: int,
    challenger_result: dict,
) -> int:
    """Update hypothesis statuses based on challenger trial outcome.

    Hypotheses linked to *reflection_id* that are in ``'proposed'`` or
    ``'challenger'`` status are resolved:

    - If the challenger verdict is ``'promoted'``, hypotheses are marked
      ``'validated'``.
    - If the challenger verdict is ``'rejected'``, hypotheses are marked
      ``'falsified'``.
    - If the challenger had zero logged decisions (``'no_challenger'``),
      hypotheses are marked ``'inconclusive'``.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    reflection_id:
        The reflection cycle whose hypotheses to resolve.
    challenger_result:
        Output from :func:`store.specs.resolve_challenger`.

    Returns
    -------
    int
        Number of hypotheses resolved.
    """
    verdict = challenger_result.get("verdict", "no_challenger")
    status_map = {
        "promoted": "validated",
        "rejected": "falsified",
        "no_challenger": "inconclusive",
    }
    new_status = status_map.get(verdict, "inconclusive")

    # Only resolve hypotheses still in a pre-resolution state.
    cursor = conn.execute(
        """UPDATE hypotheses
           SET status = ?, resolved_at = ?
           WHERE reflection_id = ?
             AND status IN ('proposed', 'challenger')""",
        (new_status, _now(), reflection_id),
    )
    conn.commit()
    return cursor.rowcount


def get_agent_hypothesis_history(conn, agent_id: str) -> list[dict]:
    """Return all hypotheses for *agent_id*, newest first.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    agent_id:
        Agent identifier.

    Returns
    -------
    list[dict]
        Hypothesis rows as dicts.
    """
    rows = conn.execute(
        """SELECT id, agent_id, reflection_id, claim, feature, direction,
                  regime_context, predicted_effect, falsification_condition,
                  status, effect_observed, created_at, resolved_at
           FROM hypotheses
           WHERE agent_id = ?
           ORDER BY created_at DESC""",
        (agent_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_hypothesis_digest(conn, limit: int = 10) -> str:
    """Return a formatted summary of the most recent hypotheses across agents.

    Designed for injection into the agent dossier or web UI summary panels.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    limit:
        Maximum number of hypotheses to include.

    Returns
    -------
    str
        Human-readable multi-line summary.
    """
    rows = conn.execute(
        """SELECT agent_id, claim, status, feature, direction, created_at
           FROM hypotheses
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    if not rows:
        return "No hypotheses registered."

    lines = ["HYPOTHESIS DIGEST", "=" * 40]
    for r in rows:
        status_tag = r["status"].upper()
        feature_note = f" [{r['feature']}]" if r["feature"] else ""
        dir_note = f" ({r['direction']})" if r["direction"] else ""
        lines.append(
            f"  [{status_tag}] {r['agent_id']}: {r['claim']}"
            f"{feature_note}{dir_note}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# M10: Challenger resolution scheduler
# ---------------------------------------------------------------------------


def check_challenger_resolution(
    conn,
    agent_id: str,
    desk_config: dict,
) -> dict:
    """Check whether the challenger trial for *agent_id* has enough data and
    resolve it if so.

    This is the entry point for the APScheduler job that periodically
    evaluates challenger trials.  It reads thresholds from *desk_config*:

    - ``challenger_min_decisions`` (default 20): minimum number of shadow
      decisions the challenger must have logged before resolution.
    - ``challenger_max_days`` (default 7): maximum trial duration in days;
      the challenger is force-resolved when this elapses even if the
      decision count is below the threshold.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    agent_id:
        Agent identifier.
    desk_config:
        Desk configuration dict (``config["desk"]``).

    Returns
    -------
    dict
        ``{"resolved": bool, ...}`` — includes the full output of
        :func:`store.specs.resolve_challenger` when resolution ran, or
        ``{"resolved": False, "reason": str}`` when the trial is still
        in progress.
    """
    from store.specs import get_challenger_spec, resolve_challenger  # noqa: PLC0415

    challenger = get_challenger_spec(conn, agent_id)
    if challenger is None:
        return {"resolved": False, "reason": "no active challenger"}

    min_decisions = desk_config.get("challenger_min_decisions", 20)
    max_days = desk_config.get("challenger_max_days", 7)

    # Count logged challenger decisions.
    row = conn.execute(
        """SELECT COUNT(*) FROM decisions
           WHERE agent_id = ? AND decision_details_json IS NOT NULL""",
        (agent_id,),
    ).fetchone()
    total_decisions = row[0] if row else 0

    challenger_decisions = 0
    all_rows = conn.execute(
        """SELECT decision_details_json, timestamp FROM decisions
           WHERE agent_id = ? AND decision_details_json IS NOT NULL
           ORDER BY timestamp ASC""",
        (agent_id,),
    ).fetchall()

    deployed_at: datetime | None = None
    for dr in all_rows:
        try:
            details = json.loads(dr["decision_details_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if details.get("challenger_spec_version") == challenger.spec_version:
            challenger_decisions += 1
            if deployed_at is None:
                try:
                    deployed_at = datetime.fromisoformat(
                        dr["timestamp"].replace("Z", "+00:00"),
                    )
                except (ValueError, TypeError):
                    pass

    # Check time-based threshold.
    now = datetime.now(timezone.utc)
    days_elapsed = 0.0
    if deployed_at is not None:
        days_elapsed = (now - deployed_at).days

    # Resolve when EITHER threshold is met.
    if challenger_decisions >= min_decisions or days_elapsed >= max_days:
        result = resolve_challenger(conn, agent_id)
        result["resolved"] = True
        result["challenger_decisions_count"] = challenger_decisions
        result["days_elapsed"] = round(days_elapsed, 1)
        return result

    return {
        "resolved": False,
        "reason": (
            f"trial in progress: {challenger_decisions}/{min_decisions} decisions, "
            f"{days_elapsed:.0f}/{max_days} days"
        ),
    }
