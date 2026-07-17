"""agents/reflection.py -- evolutionary strategy reflection engine.

Reads an agent's trade bank, decisions/counterfactuals, and evidence
dossier, calls an LLM to produce a revised strategy spec (as YAML text
output), and validates it through mechanical evidence gates -- pattern
persistence, a mandatory walk-forward backtest, and a complexity budget.
The LLM's own opinion (the adversarial pass) is advisory only: it is
recorded and surfaced in the thesis, but only the evidence gates can
reject a proposal. "The LLM proposes; the ledger disposes." An accepted
revision deploys its thesis and spec atomically via
store.specs.deploy_as_challenger() -- CHALLENGER status, not active;
challenger resolution is a later stage.

M11 feature: see docs/FORGE_PROPOSAL.md for the full design.
M10 feature: hypothesis registry for challenger trial falsification.
"""
from __future__ import annotations

import dataclasses
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
from backtest.validator import validate_spec
from backtest.walk_forward import run_walk_forward
from store.db import get_agent, get_trades
from store.specs import deploy_as_challenger, deploy_spec, get_active_spec, get_spec_history

logger = logging.getLogger(__name__)


class DeployValidationError(RuntimeError):
    """Raised when a proposed spec fails desk validation
    (backtest.validator.validate_spec) during the atomic thesis+spec
    deploy. store.specs.deploy_as_challenger does NOT raise on this itself
    -- it commits a status='rejected' spec row and returns normally, and
    its conn.commit() is the only commit in the atomic sequence, so
    letting it run unconditionally would silently finalize the
    theses/agents writes alongside a rejected spec (a real atomicity
    violation, not just a misreported result). _deploy_revision_atomically
    therefore replicates that same validation BEFORE any DB write is
    attempted, so a validation failure is caught before the transaction
    ever opens -- there is never a commit to unwind.
    """

#: Directory where versioned thesis markdown files live -- same convention as
#: store/specs.py's SPECS_DIR (module-level constant tests monkeypatch to a
#: tmp_path so reflection tests never write into the real agents/theses/).
_THESES_DIR = Path(__file__).resolve().parent / "theses"

#: Default cap on total evidence terms (entry + secondary) a proposed spec
#: may carry before "complexity must pay for itself" kicks in -- overridden
#: via desk.max_evidence_terms. See check applied in run_reflection's
#: complexity-budget gate.
DEFAULT_MAX_EVIDENCE_TERMS = 4


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
    # M10 crit 3+4 observability fields -- persisted by
    # meta/reflection_scheduler.py::run_reflection_cycle into the matching
    # reflections table columns (M8 schema). Default to None so every
    # pre-existing call site that doesn't populate them keeps working.
    research_findings_json: str | None = None
    proposed_changes: str | None = None
    adversarial_critique: str | None = None
    holdout_result: str | None = None


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
    reflection_id: int | None = None,
) -> ReflectionResult:
    """Full reflection cycle for one agent — M10 three-stage pipeline.

    Stages
    ------
    1. **Diagnose**: Build an evidence dossier from the agent's trade history,
       calibration curve, regret decisions, and regime breakdown, then call
       the LLM to produce a structured diagnosis (what's working, what's not).
       Every falsifiable hypothesis parsed out of the diagnosis is
       immediately persisted via :func:`register_hypotheses` (status
       ``proposed``) -- regardless of what Stage 2/3 later decide, so a
       Propose-call transport failure or a mechanical-gate rejection never
       silently drops a hypothesis the LLM already stated (M10 crit 6).
    2. **Propose**: Based on the diagnosis, call the LLM to generate a revised
       strategy spec (YAML).
    3. **Validate**: Run the mechanical evidence gates (pattern persistence,
       a mandatory walk-forward backtest, a complexity budget) against the
       proposed spec. The adversarial pass also runs here but is advisory
       only -- it can never block. If all evidence gates pass, the thesis
       and spec are deployed atomically as a **challenger**
       (``store.specs.deploy_as_challenger()``), and this cycle's
       hypotheses move from ``proposed`` to ``challenger`` status.

    Falls back to the legacy single-call pipeline when the dossier builder
    cannot produce a dossier (missing ledger data, no trades, etc.). The
    mechanical evidence gates still apply in full on that path. No
    hypotheses are registered on the legacy path (Stage A never runs).

    Parameters
    ----------
    reflection_id:
        The ``reflections.id`` row this cycle is writing into (created by
        the caller -- see meta/reflection_scheduler.py::run_reflection_cycle
        -- *before* calling run_reflection). Passed straight through to
        :func:`register_hypotheses` so each hypothesis is linked back to
        the reflection cycle that proposed it. ``None`` is accepted for
        ad-hoc/manual invocations that don't first insert a reflections row
        (register_hypotheses treats that as ad-hoc registration).
    """
    gates_passed: list[str] = []

    # -- Gate 0: Benchmark agents never reflect --------------------------------
    # Benchmark agents (id starts with "benchmark_") are permanent baselines --
    # their trade history is the null distribution every significance test is
    # measured against. meta/reflection_scheduler.py::check_agent_eligible
    # blocks the scheduled path; this guard protects the manual single-agent
    # web trigger too, which calls run_reflection directly and does not go
    # through check_agent_eligible.
    if agent_id.startswith("benchmark_"):
        return ReflectionResult(
            triggered=False,
            new_spec_yaml=None,
            spec_version=None,
            deployed=False,
            rejection_reason=None,
            blocked_by_gate="benchmark agent — permanent baseline, never reflects",
            adversarial_flaws=[],
            gates_passed=[],
        )

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

    # -- Gate 2: Update throttle ----------------------------------------------
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
    hypotheses: list[dict] = []
    hypothesis_ids: list[int] = []
    try:
        dossier_ledger_dir = config.get("ledger_dir", "ledger")
        dossier_train_path = config.get(
            "training_dataset_path",
            "data/historical_data/training_dataset.parquet",
        )
        dossier = build_dossier(conn, agent_id, dossier_ledger_dir, dossier_train_path)
    except Exception as exc:
        logger.warning("Dossier build failed for %s: %s — falling back to legacy path", agent_id, exc)

    # research_findings_json: a compact structured digest of the dossier
    # (not the whole rendered prompt) -- persisted to reflections.
    # research_findings_json by meta/reflection_scheduler.py for post-hoc
    # audit of what evidence informed this cycle, regardless of outcome.
    research_findings_json = json.dumps(_dossier_digest(dossier)) if dossier is not None else None

    def _call_llm(
        system_prompt: str, user_prompt: str,
    ) -> tuple[str | None, ReflectionResult | None]:
        """Invoke ``llm_fn``, catching only transport failures (network
        error, exhausted mock, timeout) -- these must not escape
        run_reflection as an unhandled exception; they should surface as a
        normal rejected cycle so the scheduler can log it and move on to the
        next agent. Prompt-building bugs are NOT caught here -- they
        propagate to the caller so they aren't mislabeled as transport
        failures.
        """
        try:
            return llm_fn(system_prompt, user_prompt), None
        except Exception as exc:
            logger.warning(
                "[%s] reflection LLM transport failed mid-pipeline: %s", agent_id, exc,
            )
            return None, ReflectionResult(
                triggered=True,
                new_spec_yaml=None,
                spec_version=None,
                deployed=False,
                rejection_reason=f"reflection LLM transport failed: {exc}",
                blocked_by_gate=None,
                adversarial_flaws=[],
                gates_passed=gates_passed,
                research_findings_json=research_findings_json,
            )

    if dossier is not None and dossier.closed_trades:
        # Three-stage pipeline: Diagnose → Propose → Validate
        diagnose_prompt = _build_diagnose_prompt(agent_id, dossier)
        diagnosis, err = _call_llm(_REFLECTION_SYSTEM_PROMPT, diagnose_prompt)
        if err is not None:
            return err
        hypotheses = _parse_diagnose_hypotheses(diagnosis)
        logger.info(
            "[%s] Stage 1 (Diagnose) complete — %d chars, %d hypotheses",
            agent_id, len(diagnosis), len(hypotheses),
        )

        # M10 crit 6: register hypotheses the moment Stage A states them --
        # persisted as 'proposed' regardless of what Stage 2/3 do next.
        hypothesis_ids = register_hypotheses(conn, agent_id, reflection_id, hypotheses)

        # Stage 2: Propose — generate revised spec informed by diagnosis
        propose_prompt = _build_propose_prompt(
            agent_id, diagnosis, current_spec, current_version,
        )
        llm_response, err = _call_llm(_REFLECTION_SYSTEM_PROMPT, propose_prompt)
        if err is not None:
            return err
        logger.info(
            "[%s] Stage 2 (Propose) complete — %d chars",
            agent_id, len(llm_response),
        )
    else:
        # Legacy single-call pipeline (fallback)
        prompt = build_reflection_prompt(
            agent_id, trades, decisions, {}, current_spec,
        )
        llm_response, err = _call_llm(_REFLECTION_SYSTEM_PROMPT, prompt)
        if err is not None:
            return err

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
            research_findings_json=research_findings_json,
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
            research_findings_json=research_findings_json,
        )
    gates_passed.append("zero_evidence_guard")

    # proposed_changes: Stage-A hypotheses + thesis/spec diff summary --
    # persisted to reflections.proposed_changes regardless of what happens
    # next (mechanical gates below may still reject this proposal, but the
    # LLM's proposal itself is worth auditing either way).
    change_summary_text = _spec_change_summary(current_spec, revised_spec)
    proposed_changes_json = json.dumps({
        "hypotheses": hypotheses,
        "thesis_diff_summary": change_summary_text,
        "spec_diff_summary": _spec_diff_summary(current_spec, revised_spec),
    })

    # ------------------------------------------------------------------
    # Stage 3: Validate — mechanical evidence gates decide; the LLM's own
    # opinion (adversarial pass) is advisory only. "The LLM proposes; the
    # ledger disposes."
    # ------------------------------------------------------------------

    # -- Adversarial pass (ADVISORY — never blocks) ---------------------------
    # A CRITICAL finding is recorded for observability and appended to the
    # deployed thesis as a "Known weaknesses" section, but only mechanical
    # evidence gates (pattern persistence, walk-forward, complexity budget)
    # below can reject a proposal. blocked_by_gate="adversarial_pass" is no
    # longer possible.
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
            research_findings_json=research_findings_json,
            proposed_changes=proposed_changes_json,
        )
    adversarial_critique_text: str | None = None
    if critical_flaw:
        adversarial_critique_text = (
            "\n".join(f"- {f}" for f in flaws)
            if flaws else "critical flaw found by adversarial pass"
        )
        logger.info(
            "[%s] adversarial pass found CRITICAL findings — recorded as advisory,"
            " mechanical gates still decide", agent_id,
        )
    gates_passed.append("adversarial_advisory")

    # -- Gate 3: Pattern persistence --------------------------------------------
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
            research_findings_json=research_findings_json,
            proposed_changes=proposed_changes_json,
            adversarial_critique=adversarial_critique_text,
        )
    gates_passed.append("pattern_persistence")

    # -- Gate 4: Walk-forward backtest (MANDATORY) -------------------------------
    # Real out-of-sample validation = this test window + the challenger
    # trial. A missing/too-short ledger or any exception from
    # run_walk_forward is a hard, logged rejection -- never a silent skip
    # that lets an unvalidated spec through to deploy. Unconditional: no
    # incumbent spec is not an excuse to skip validating the proposal.
    try:
        ledger_dir_path = Path(config["ledger_dir"])
        taker_fee = config.get("taker_fee", 0.00035)
        wf_report = run_walk_forward(revised_spec, ledger_dir_path, taker_fee)
    except Exception as exc:
        logger.warning("[%s] walk-forward gate failed: %s", agent_id, exc)
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason=f"walk-forward validation failed: {exc}",
            blocked_by_gate="walk_forward",
            adversarial_flaws=flaws,
            gates_passed=gates_passed,
            research_findings_json=research_findings_json,
            proposed_changes=proposed_changes_json,
            adversarial_critique=adversarial_critique_text,
        )

    holdout_result_json = json.dumps(_walk_forward_digest(wf_report))

    if wf_report.deflated_sharpe <= 0:
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason=(
                f"walk-forward deflated Sharpe {wf_report.deflated_sharpe:.3f} <= 0"
            ),
            blocked_by_gate="walk_forward",
            adversarial_flaws=flaws,
            gates_passed=gates_passed,
            research_findings_json=research_findings_json,
            proposed_changes=proposed_changes_json,
            adversarial_critique=adversarial_critique_text,
            holdout_result=holdout_result_json,
        )
    if _walk_forward_is_fragile(wf_report):
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason=(
                "walk-forward parameter-sensitivity sweep flags this spec as"
                " fragile — a 20% parameter perturbation flips it unprofitable"
            ),
            blocked_by_gate="walk_forward",
            adversarial_flaws=flaws,
            gates_passed=gates_passed,
            research_findings_json=research_findings_json,
            proposed_changes=proposed_changes_json,
            adversarial_critique=adversarial_critique_text,
            holdout_result=holdout_result_json,
        )
    gates_passed.append("walk_forward")

    # -- Gate 5: Complexity budget -------------------------------------------
    # "Complexity must pay for itself": a proposal over the absolute cap, or
    # carrying more evidence terms than the incumbent, must beat the
    # incumbent's walk-forward deflated Sharpe. With no incumbent, only the
    # absolute cap applies (nothing to beat).
    max_evidence_terms = config["desk"].get(
        "max_evidence_terms", DEFAULT_MAX_EVIDENCE_TERMS,
    )
    proposed_terms = len(revised_spec.evidence) + len(revised_spec.secondary_evidence)
    incumbent_terms = (
        len(current_spec.evidence) + len(current_spec.secondary_evidence)
        if current_spec is not None else 0
    )
    exceeds_cap = proposed_terms > max_evidence_terms
    exceeds_incumbent = current_spec is not None and proposed_terms > incumbent_terms

    if exceeds_cap or exceeds_incumbent:
        if current_spec is None:
            return ReflectionResult(
                triggered=True,
                new_spec_yaml=propose_yaml,
                spec_version=revised_spec.spec_version,
                deployed=False,
                rejection_reason=(
                    f"{proposed_terms} evidence terms exceeds the complexity"
                    f" budget ({max_evidence_terms}) with no incumbent to"
                    " compare against"
                ),
                blocked_by_gate="complexity_budget",
                adversarial_flaws=flaws,
                gates_passed=gates_passed,
                research_findings_json=research_findings_json,
                proposed_changes=proposed_changes_json,
                adversarial_critique=adversarial_critique_text,
                holdout_result=holdout_result_json,
            )
        try:
            incumbent_wf_report = run_walk_forward(current_spec, ledger_dir_path, taker_fee)
        except Exception as exc:
            logger.warning(
                "[%s] complexity-budget incumbent walk-forward failed: %s", agent_id, exc,
            )
            return ReflectionResult(
                triggered=True,
                new_spec_yaml=propose_yaml,
                spec_version=revised_spec.spec_version,
                deployed=False,
                rejection_reason=(
                    f"complexity budget: could not evaluate incumbent for"
                    f" comparison: {exc}"
                ),
                blocked_by_gate="complexity_budget",
                adversarial_flaws=flaws,
                gates_passed=gates_passed,
                research_findings_json=research_findings_json,
                proposed_changes=proposed_changes_json,
                adversarial_critique=adversarial_critique_text,
                holdout_result=holdout_result_json,
            )
        if wf_report.deflated_sharpe <= incumbent_wf_report.deflated_sharpe:
            reason_bits = []
            if exceeds_cap:
                reason_bits.append(f"{proposed_terms} terms exceeds cap {max_evidence_terms}")
            if exceeds_incumbent:
                reason_bits.append(f"{proposed_terms} terms exceeds incumbent's {incumbent_terms}")
            return ReflectionResult(
                triggered=True,
                new_spec_yaml=propose_yaml,
                spec_version=revised_spec.spec_version,
                deployed=False,
                rejection_reason=(
                    f"complexity budget: {' and '.join(reason_bits)}, and"
                    f" proposal's deflated Sharpe {wf_report.deflated_sharpe:.3f}"
                    f" does not beat incumbent's"
                    f" {incumbent_wf_report.deflated_sharpe:.3f}"
                ),
                blocked_by_gate="complexity_budget",
                adversarial_flaws=flaws,
                gates_passed=gates_passed,
                research_findings_json=research_findings_json,
                proposed_changes=proposed_changes_json,
                adversarial_critique=adversarial_critique_text,
                holdout_result=holdout_result_json,
            )
    gates_passed.append("complexity_budget")

    # -- All gates passed → atomically deploy thesis + spec as challenger -----
    # M10 crit 5 contract: an accepted revision becomes CHALLENGER (shadow
    # evaluation), not active — challenger resolution is a later task.
    agent_row = get_agent(conn, agent_id) or {}
    current_thesis_version = agent_row.get("current_thesis_version", 1)
    next_thesis_version = current_thesis_version + 1
    previous_thesis_text = dossier.thesis_text if dossier is not None else ""
    thesis_markdown = _build_revised_thesis_markdown(
        agent_id,
        next_thesis_version,
        previous_thesis_text,
        diagnosis,
        change_summary_text,
        flaws if critical_flaw else [],
    )

    try:
        spec_id = _deploy_revision_atomically(
            conn,
            agent_id,
            revised_spec,
            thesis_markdown,
            next_thesis_version,
            change_summary_text,
            adversarial_critique_text,
            config["desk"],
        )
    except DeployValidationError as exc:
        logger.warning("[%s] revision rejected by desk validation: %s", agent_id, exc)
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason=str(exc),
            blocked_by_gate="desk_validation",
            adversarial_flaws=flaws,
            gates_passed=gates_passed,
            research_findings_json=research_findings_json,
            proposed_changes=proposed_changes_json,
            adversarial_critique=adversarial_critique_text,
            holdout_result=holdout_result_json,
        )
    except Exception as exc:
        logger.warning("[%s] atomic thesis+spec deploy failed: %s", agent_id, exc)
        return ReflectionResult(
            triggered=True,
            new_spec_yaml=propose_yaml,
            spec_version=revised_spec.spec_version,
            deployed=False,
            rejection_reason=f"atomic thesis+spec deploy failed: {exc}",
            blocked_by_gate=None,
            adversarial_flaws=flaws,
            gates_passed=gates_passed,
            research_findings_json=research_findings_json,
            proposed_changes=proposed_changes_json,
            adversarial_critique=adversarial_critique_text,
            holdout_result=holdout_result_json,
        )

    logger.info(
        "[%s] revision v%d deployed as challenger (specs.id=%d, thesis v%d)",
        agent_id, revised_spec.spec_version, spec_id, next_thesis_version,
    )

    # M10 crit 6: this cycle's hypotheses now have a live challenger trial
    # to be judged by -- move them out of 'proposed' into 'challenger'.
    if hypothesis_ids:
        placeholders = ",".join("?" * len(hypothesis_ids))
        conn.execute(
            f"UPDATE hypotheses SET status = 'challenger' WHERE id IN ({placeholders})",
            hypothesis_ids,
        )
        conn.commit()

    return ReflectionResult(
        triggered=True,
        new_spec_yaml=propose_yaml,
        spec_version=revised_spec.spec_version,
        deployed=True,
        rejection_reason=None,
        blocked_by_gate=None,
        adversarial_flaws=flaws if flaws else [],
        gates_passed=gates_passed,
        research_findings_json=research_findings_json,
        proposed_changes=proposed_changes_json,
        adversarial_critique=adversarial_critique_text,
        holdout_result=holdout_result_json,
    )


# ---------------------------------------------------------------------------
# M10 crit 3: mechanical-gate helpers (walk-forward digest, fragility)
# ---------------------------------------------------------------------------


def _dossier_digest(dossier: Any) -> dict:
    """Compact structured digest of the evidence dossier used for a
    reflection cycle -- stored in reflections.research_findings_json.
    Deliberately NOT the full rendered prompt (that's dossier.to_prompt());
    just enough to audit after the fact what evidence informed the LLM.
    """
    return {
        "closed_trades": len(dossier.closed_trades),
        "calibration_buckets": len(dossier.calibration_curve),
        "high_regret_decisions": len(dossier.high_regret_decisions),
        "regimes": list(dossier.win_rate_by_regime.keys()),
        "features_tracked": list(dossier.feature_stats.keys()),
        "hypothesis_track_record_count": len(dossier.hypothesis_track_record),
    }


def _spec_change_summary(current_spec: Spec | None, revised_spec: Spec) -> str:
    """One-line human-readable summary of the proposed spec revision --
    used both as theses.change_summary and inside proposed_changes_json's
    thesis_diff_summary."""
    from_version = current_spec.spec_version if current_spec else 0
    return (
        f"spec v{from_version} -> v{revised_spec.spec_version}: "
        f"evidence={[e.name for e in revised_spec.evidence]}, "
        f"SL={revised_spec.stop_loss_pct} TP={revised_spec.take_profit_pct} "
        f"leverage={revised_spec.leverage}x size={revised_spec.position_size_pct:.0%}"
    )


def _spec_diff_summary(current_spec: Spec | None, revised_spec: Spec) -> dict:
    """Structured spec-diff summary -- the spec_diff_summary half of
    proposed_changes_json (the other half is the Stage-A hypotheses)."""
    return {
        "from_version": current_spec.spec_version if current_spec else None,
        "to_version": revised_spec.spec_version,
        "evidence_terms": [e.name for e in revised_spec.evidence],
        "secondary_evidence_terms": [e.name for e in revised_spec.secondary_evidence],
        "stop_loss_pct": revised_spec.stop_loss_pct,
        "take_profit_pct": revised_spec.take_profit_pct,
        "leverage": revised_spec.leverage,
        "position_size_pct": revised_spec.position_size_pct,
    }


def _walk_forward_digest(wf_report: Any) -> dict:
    """Summarize a WalkForwardReport for reflections.holdout_result --
    the deflated Sharpe, per-window Sharpe, trade counts, and the raw
    parameter-sensitivity sweep."""
    return {
        "deflated_sharpe": wf_report.deflated_sharpe,
        "train_sharpe": wf_report.train.sharpe,
        "validate_sharpe": wf_report.validate.sharpe,
        "test_sharpe": wf_report.test.sharpe,
        "train_trades": len(wf_report.train.trades),
        "validate_trades": len(wf_report.validate.trades),
        "test_trades": len(wf_report.test.trades),
        "parameter_sensitivity": wf_report.parameter_sensitivity,
    }


def _walk_forward_is_fragile(wf_report: Any) -> bool:
    """A strategy is fragility-flagged when a modest (20%) perturbation of
    any risk/entry parameter (see backtest/walk_forward.py's
    PERTURBATION_PCT sweep) flips it from profitable to unprofitable in the
    walk-forward test window -- the edge doesn't survive small parameter
    jitter, a classic overfitting signature. WalkForwardReport has no
    dedicated fragility field; this derives the verdict from
    parameter_sensitivity (each perturbed param's resulting Sharpe delta).
    """
    base_sharpe = wf_report.test.sharpe
    if base_sharpe <= 0:
        return False  # already failing on its own basis -- not a fragility verdict
    return any(
        base_sharpe + delta <= 0
        for delta in wf_report.parameter_sensitivity.values()
    )


# ---------------------------------------------------------------------------
# M10 crit 3: Stage 1 (Diagnose) as a standalone, testable function
# ---------------------------------------------------------------------------


def diagnose(
    agent_id: str,
    dossier: Any,
    llm_fn: Callable[[str, str], str],
) -> tuple[str, list[dict]]:
    """Stage 1 of the reflection pipeline: build the Diagnose prompt from
    the evidence dossier, call the LLM, and parse falsifiable hypotheses
    out of its response.

    Returns ``(raw_diagnosis_text, hypotheses)``. Each hypothesis dict has
    ``claim``, ``evidence_refs``, ``predicted_effect``, and
    ``falsification_condition`` keys. A diagnosis with no parseable
    hypotheses block still returns the raw text with an empty hypotheses
    list -- Stage 2 doesn't require Stage 1 to have parsed any.
    """
    prompt = _build_diagnose_prompt(agent_id, dossier)
    raw = llm_fn(_REFLECTION_SYSTEM_PROMPT, prompt)
    return raw, _parse_diagnose_hypotheses(raw)


def _extract_json_block(text: str) -> Any | None:
    """Parse *text* as JSON directly, then fall back to a fenced ```json
    block. Returns ``None`` if neither yields valid JSON (e.g. a
    plain-text-only diagnosis with no hypotheses block)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def _parse_diagnose_hypotheses(raw_text: str) -> list[dict]:
    """Parse the Diagnose stage's LLM output into structured falsifiable
    hypotheses (claim, evidence_refs, predicted_effect,
    falsification_condition). Returns ``[]`` for plain-text diagnoses that
    carry no hypotheses block -- this is a valid, non-error outcome."""
    parsed = _extract_json_block(raw_text)
    if isinstance(parsed, dict):
        candidates = parsed.get("hypotheses")
    elif isinstance(parsed, list):
        candidates = parsed
    else:
        candidates = None
    if not isinstance(candidates, list):
        return []

    hypotheses: list[dict] = []
    for h in candidates:
        if not isinstance(h, dict) or not h.get("claim"):
            continue
        hypotheses.append({
            "claim": h["claim"],
            "evidence_refs": h.get("evidence_refs") or [],
            "predicted_effect": h.get("predicted_effect", ""),
            "falsification_condition": h.get("falsification_condition", ""),
        })
    return hypotheses


# ---------------------------------------------------------------------------
# M10 crit 4: atomic thesis + spec co-revision
# ---------------------------------------------------------------------------


def _build_revised_thesis_markdown(
    agent_id: str,
    next_thesis_version: int,
    previous_thesis_text: str,
    diagnosis: str,
    change_summary: str,
    critical_flaws: list[str],
) -> str:
    """Build the revised thesis markdown written to
    agents/theses/{agent_id}_v{N+1}.md: the previous thesis text (if any)
    with a new revision section appended, plus a "## Known weaknesses"
    section when the adversarial pass found CRITICAL findings (req 2b) --
    advisory input made visible in the thesis, never a deploy blocker.
    """
    lines: list[str] = []
    if previous_thesis_text.strip():
        lines.append(previous_thesis_text.rstrip())
        lines.append("")
    lines.append(f"## Revision v{next_thesis_version} — {_now()}")
    lines.append("")
    lines.append("### Diagnosis")
    lines.append(diagnosis.strip() or "(no diagnosis text captured)")
    lines.append("")
    lines.append("### Changes")
    lines.append(change_summary.strip() or "(spec revised — see deployed YAML for details)")
    if critical_flaws:
        lines.append("")
        lines.append("## Known weaknesses")
        for flaw in critical_flaws:
            lines.append(f"- {flaw}")
    return "\n".join(lines) + "\n"


def _insert_thesis_row(
    conn,
    agent_id: str,
    version: int,
    text: str,
    change_summary: str,
    adversarial_critique: str | None,
) -> int:
    """Insert a theses row. Factored out of _deploy_revision_atomically so
    a test can monkeypatch this specific step to force a mid-deploy
    failure (the alternative to monkeypatching deploy_as_challenger)."""
    cursor = conn.execute(
        """INSERT INTO theses (agent_id, version, text, created_at,
                                change_summary, adversarial_critique)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent_id, version, text, _now(), change_summary, adversarial_critique),
    )
    return cursor.lastrowid


def _deploy_revision_atomically(
    conn,
    agent_id: str,
    revised_spec: Spec,
    thesis_markdown: str,
    next_thesis_version: int,
    change_summary: str,
    adversarial_critique: str | None,
    desk_config: dict | None,
) -> int:
    """Atomically deploy an accepted reflection revision: write the revised
    thesis file, insert its theses row, bump
    agents.current_thesis_version, and deploy the spec as a challenger
    (thesis_version stamped to the new version) -- all four together or
    none.

    All DB writes execute in one uncommitted transaction on *conn*. The
    thesis file is written before the transaction's only commit (inside
    deploy_as_challenger, the final step) so a failure at any step —
    including deploy_as_challenger itself — can be fully unwound: the
    thesis file is deleted and the transaction rolled back before
    re-raising, leaving thesis version, specs, theses, and the filesystem
    exactly as they were.

    IMPORTANT: deploy_as_challenger does NOT raise when the spec fails
    desk validation (e.g. leverage over the desk cap) -- it commits a
    status='rejected' spec row and returns normally, and that commit is
    the only commit in this whole sequence. If that were allowed to run
    unconditionally, its conn.commit() would silently finalize the
    theses/agents writes above alongside a rejected spec: a genuine
    atomicity violation that a post-hoc rollback CANNOT undo (the commit
    already happened by the time any status check could run). So the
    identical validation deploy_as_challenger performs internally
    (backtest.validator.validate_spec, same spec + desk_config) runs here
    FIRST, before any DB write is attempted -- a validation failure raises
    DeployValidationError before the transaction ever opens.
    """
    spec_to_deploy = dataclasses.replace(
        revised_spec, thesis_version=next_thesis_version,
    )

    if desk_config is not None:
        pre_flight_errors = validate_spec(spec_to_deploy, desk_config)
        if pre_flight_errors:
            raise DeployValidationError(
                "spec failed desk validation: " + "; ".join(pre_flight_errors)
            )

    _THESES_DIR.mkdir(parents=True, exist_ok=True)
    thesis_path = _THESES_DIR / f"{agent_id}_v{next_thesis_version}.md"

    try:
        thesis_path.write_text(thesis_markdown, encoding="utf-8")

        _insert_thesis_row(
            conn, agent_id, next_thesis_version, thesis_markdown,
            change_summary, adversarial_critique,
        )
        conn.execute(
            "UPDATE agents SET current_thesis_version = ? WHERE id = ?",
            (next_thesis_version, agent_id),
        )

        spec_id = deploy_as_challenger(conn, agent_id, spec_to_deploy, desk_config)

        # Defence-in-depth invariant check, not the primary guarantee: the
        # pre-flight validation above uses the identical (spec,
        # desk_config) pair deploy_as_challenger validates internally, so
        # this should never fire. Kept because store/specs.py is another
        # task's in-flight work -- if its validation logic ever diverges
        # from what's replicated above, surface that loudly instead of
        # silently reporting deployed=True for a rejected spec. Rolling
        # back here is best-effort only: deploy_as_challenger's commit
        # (above) has already landed by this point, so if this ever
        # fires it means thesis_version/theses may already be
        # permanently committed alongside a non-challenger spec --
        # a real bug requiring investigation, not something this
        # rollback call can undo after the fact.
        row = conn.execute(
            "SELECT status, rejection_reason, validation_errors FROM specs WHERE id = ?",
            (spec_id,),
        ).fetchone()
        if row is None or row["status"] != "challenger":
            detail = (
                (row["validation_errors"] or row["rejection_reason"]) if row
                else "spec row missing"
            )
            raise DeployValidationError(
                f"deploy_as_challenger produced status="
                f"{row['status'] if row else 'missing'} instead of challenger: {detail}"
            )
    except Exception:
        if thesis_path.exists():
            thesis_path.unlink()
        conn.rollback()
        raise

    return spec_id


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
        "After your written diagnosis, output a fenced JSON block with 2-3",
        "falsifiable hypotheses drawn from the evidence above, in this shape:",
        "",
        "```json",
        '{"hypotheses": [',
        '  {"claim": "...", "evidence_refs": ["...dossier section refs..."],',
        '   "predicted_effect": "...", "falsification_condition": "..."}',
        "]}",
        "```",
        "",
        "Do NOT include a YAML spec yet — that is Stage 2.",
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


def check_update_throttle(
    conn, agent_id: str, min_trades_since: int = 30, min_days: int = 14,
) -> tuple[bool, str | None]:
    """Gate 2: Max one update per *min_trades_since* trades OR *min_days* days.

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
    """Gate 3: A condition's feature must have trade evidence spanning
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
    """Second LLM call plays devil's advocate -- ADVISORY only (M10 crit 3):
    the caller (run_reflection) records a critical finding for observability
    and appends it to the deployed thesis's "Known weaknesses" section, but
    never blocks a deploy on it. Only mechanical evidence gates decide.

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

    Delegates to :func:`store.performance.compute_calibration_curve`.
    """
    from store.performance import compute_calibration_curve as _inner

    curve_dict = _inner(conn, agent_id)
    results = []
    for bucket_label in sorted(curve_dict):
        entry = curve_dict[bucket_label]
        results.append({
            "bucket": bucket_label,
            "count": entry["sample_count"],
            "win_rate": entry["realized_wr"],
        })
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
    """Update hypothesis statuses based on a challenger trial outcome.

    Hypotheses linked to *reflection_id* that are still in ``'proposed'``
    or ``'challenger'`` status are resolved. Mechanical interpretation of
    "predicted effect realized" (T8): a reflection cycle's hypotheses are
    the claims that motivated ITS spec revision, and the challenger trial
    is the out-of-sample test of that whole revision -- so the trial
    verdict stands in for per-hypothesis direction-matching, which Stage A
    doesn't currently parse a structured direction for anyway:

    - ``'promoted'`` (challenger's mean regret beat the incumbent's) →
      the predicted effect was realized → ``'validated'``.
    - ``'rejected'`` → per spec text, "challenger rejected" alone is
      sufficient to falsify → ``'falsified'`` (no separate
      falsification_condition text-matching is attempted).
    - ``'not_resolvable'`` or ``'no_challenger'`` (no labeled evidence, or
      the trial window expired without enough signal) → ``'inconclusive'``.

    ``effect_observed`` is recorded in every case it can be computed: the
    regret improvement the challenger achieved over the incumbent
    (``incumbent_mean_regret - challenger_mean_regret`` from
    :func:`store.specs.resolve_challenger`'s result -- positive means the
    challenger reduced regret). It is left ``NULL`` only when
    *challenger_result* carries no regret numbers at all (the
    zero-evidence / not-resolvable case).

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
        "not_resolvable": "inconclusive",
        "no_challenger": "inconclusive",
    }
    new_status = status_map.get(verdict, "inconclusive")

    incumbent_mean = challenger_result.get("incumbent_mean_regret")
    challenger_mean = challenger_result.get("challenger_mean_regret")
    effect_observed = None
    if isinstance(incumbent_mean, (int, float)) and isinstance(challenger_mean, (int, float)):
        effect_observed = round(incumbent_mean - challenger_mean, 4)

    # Only resolve hypotheses still in a pre-resolution state.
    cursor = conn.execute(
        """UPDATE hypotheses
           SET status = ?, effect_observed = ?, resolved_at = ?
           WHERE reflection_id = ?
             AND status IN ('proposed', 'challenger')""",
        (new_status, effect_observed, _now(), reflection_id),
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


def get_hypothesis_track_record(conn, agent_id: str) -> list[dict]:
    """Return the agent's hypothesis track record for dossier injection.

    Delegates to :func:`get_agent_hypothesis_history` — the public API
    name expected by the dossier builder and the web UI summary panels.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    agent_id:
        Agent identifier.

    Returns
    -------
    list[dict]
        Hypothesis rows as dicts (newest first), suitable for display in
        the dossier or the agent page hypothesis panel.
    """
    return get_agent_hypothesis_history(conn, agent_id)


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
    evaluates challenger trials (forge.py's ``challenger_resolution`` job).
    It reads thresholds from *desk_config*:

    - ``challenger_min_decisions`` (default 20): minimum number of
      **labeled** shadow decisions (T8 -- joined against
      ``decision_labels`` at :data:`store.specs.RESOLUTION_HORIZON`, not
      raw ``decisions`` rows) the challenger must have before resolution.
      An unlabeled shadow row (the nightly labeling job hasn't caught up
      yet) does not count.
    - ``challenger_max_days`` (default 7): maximum trial duration in days,
      measured from the challenger's ``specs.deployed_at``; the challenger
      is force-resolved when this elapses even if the labeled-decision
      count is below the threshold.

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
    from store.specs import (  # noqa: PLC0415
        RESOLUTION_HORIZON,
        get_challenger_spec,
        resolve_challenger,
    )

    challenger = get_challenger_spec(conn, agent_id)
    if challenger is None:
        return {"resolved": False, "reason": "no active challenger"}

    min_decisions = desk_config.get("challenger_min_decisions", 20)
    max_days = desk_config.get("challenger_max_days", 7)

    # deployed_at comes straight from the specs row -- the authoritative
    # trial start (matches resolve_challenger's own trial-window scoping),
    # not inferred from the first matching decisions row (which lags the
    # true deploy time and would undercount days_elapsed).
    trial_row = conn.execute(
        """SELECT deployed_at FROM specs
           WHERE agent_id = ? AND status = 'challenger' AND spec_version = ?
           ORDER BY id DESC LIMIT 1""",
        (agent_id, challenger.spec_version),
    ).fetchone()
    deployed_at_str = trial_row["deployed_at"] if trial_row else None

    challenger_labeled = 0
    if deployed_at_str is not None:
        rows = conn.execute(
            """SELECT d.decision_details_json
               FROM decisions d
               JOIN decision_labels dl
                 ON dl.decision_id = d.id AND dl.horizon = ?
               WHERE d.agent_id = ? AND d.timestamp >= ?
                 AND dl.regret_pct IS NOT NULL""",
            (RESOLUTION_HORIZON, agent_id, deployed_at_str),
        ).fetchall()
        for row in rows:
            try:
                details = json.loads(row["decision_details_json"]) if row["decision_details_json"] else {}
            except (json.JSONDecodeError, TypeError):
                details = {}
            if details.get("challenger_spec_version") == challenger.spec_version:
                challenger_labeled += 1

    # Time-based threshold, measured fractionally (not truncated to whole
    # days) so a trial deployed 6h ago never misreads as "0 days elapsed".
    days_elapsed = 0.0
    if deployed_at_str is not None:
        try:
            deployed_at = datetime.fromisoformat(deployed_at_str.replace("Z", "+00:00"))
            days_elapsed = (
                datetime.now(timezone.utc) - deployed_at
            ).total_seconds() / 86400.0
        except (ValueError, TypeError):
            pass

    # Resolve when EITHER threshold is met. window_expired tracks whether
    # the max_days threshold specifically is what's firing -- only in that
    # case is it safe to force-close a zero-evidence trial (T8 review
    # Finding 2). The min_decisions threshold alone (challenger side has
    # enough labeled decisions) says nothing about the incumbent side, so a
    # not_resolvable verdict reached via that path is still genuinely
    # in-progress, not expired.
    window_expired = days_elapsed >= max_days
    if challenger_labeled >= min_decisions or window_expired:
        result = resolve_challenger(conn, agent_id, force_close=window_expired)

        if result.get("verdict") == "not_resolvable":
            # This only happens when window_expired is False (force_close
            # was False): the min_decisions threshold fired on the
            # challenger side alone while the incumbent side still has zero
            # labeled decisions in the trial window. That is NOT a window
            # expiry -- the trial is still genuinely in progress, so leave
            # both the spec row and the hypotheses untouched (T8 review
            # Finding 2, invariant (a)) rather than reporting "resolved".
            return {
                "resolved": False,
                "reason": (
                    f"trial in progress: {challenger_labeled}/{min_decisions} labeled"
                    f" decisions, {days_elapsed:.1f}/{max_days} days"
                ),
            }

        result["resolved"] = True
        result["challenger_labeled_decisions_count"] = challenger_labeled
        result["days_elapsed"] = round(days_elapsed, 1)
        return result

    return {
        "resolved": False,
        "reason": (
            f"trial in progress: {challenger_labeled}/{min_decisions} labeled decisions, "
            f"{days_elapsed:.1f}/{max_days} days"
        ),
    }


def apply_challenger_resolution(
    conn,
    agent_id: str,
    desk_config: dict,
) -> dict:
    """Run one full challenger-resolution pass for *agent_id* -- the
    per-agent body of forge.py's hourly ``challenger_resolution`` job,
    extracted here so it is importable and behaviorally testable (forge.py
    imports apscheduler at module load, unavailable in the test env).

    When :func:`check_challenger_resolution` reports the trial resolved,
    performs the spec's TWO unconditional actions (FORGE_PROPOSAL.md:1183 --
    "Either way the outcome lands in reflections AND resolves the cycle's
    hypotheses"):

    1. Resolves every reflection cycle whose hypotheses are still
       ``status='challenger'`` for this agent (via
       :func:`resolve_hypotheses`).
    2. Writes ``reflections.outcome = "challenger_{verdict}"``. T8 review
       r2 Fix B: this write is NOT gated on the cycle having registered
       hypotheses -- a cycle with zero hypotheses (legacy pipeline path, or
       Stage A parsed none) previously never got its outcome recorded and
       showed PENDING on the agent page forever. When no hypothesis rows
       link a reflection cycle to this trial, the outcome lands on the most
       recent ``outcome='deployed'`` reflections row for the agent -- the
       cycle that deployed this challenger (run_reflection_cycle writes
       ``'deployed'`` on every successful challenger deploy, scheduled and
       web-triggered alike).

    Returns
    -------
    dict
        :func:`check_challenger_resolution`'s result, unchanged.
    """
    result = check_challenger_resolution(conn, agent_id, desk_config)
    if not result.get("resolved"):
        return result

    hyp_rows = conn.execute(
        """SELECT DISTINCT reflection_id FROM hypotheses
           WHERE agent_id = ? AND status = 'challenger'
             AND reflection_id IS NOT NULL""",
        (agent_id,),
    ).fetchall()
    reflection_ids = [hr["reflection_id"] for hr in hyp_rows]

    for reflection_id in reflection_ids:
        resolve_hypotheses(conn, reflection_id, result)

    if not reflection_ids:
        # Zero-hypotheses cycle: fall back to the reflections row that
        # deployed this challenger.
        row = conn.execute(
            """SELECT id FROM reflections
               WHERE agent_id = ? AND outcome = 'deployed'
               ORDER BY id DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if row is not None:
            reflection_ids = [row["id"]]
        else:
            logger.warning(
                "[%s] Challenger resolved (verdict=%s) but no reflections row "
                "found to record the outcome on (no challenger-status "
                "hypotheses and no 'deployed' reflection)",
                agent_id, result.get("verdict"),
            )

    outcome = f"challenger_{result.get('verdict')}"
    for reflection_id in reflection_ids:
        conn.execute(
            "UPDATE reflections SET outcome = ? WHERE id = ?",
            (outcome, reflection_id),
        )
    conn.commit()
    return result
