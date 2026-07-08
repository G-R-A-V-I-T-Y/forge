"""backtest/interpreter.py -- evaluates a Spec against one feature row.

Deterministic, pure, no I/O. Produces the same {action, confidence,
evidence_strength} shape an LLM decision already produces (see
agents/decision_loop.py's run_decision -- response.get("confidence"),
response.get("evidence_strength")), so a compiled agent can eventually call
this instead of an LLM (M8, out of scope here) with zero downstream changes.
"""
from __future__ import annotations

from backtest.dsl import EvidenceTerm, Spec, Threshold

_OPS = {
    ">": lambda v, x: v > x,
    ">=": lambda v, x: v >= x,
    "<": lambda v, x: v < x,
    "<=": lambda v, x: v <= x,
    "==": lambda v, x: v == x,
    "between": lambda v, x: x[0] <= v <= x[1],
}


def _threshold_weight(thresholds: list[Threshold], value: float) -> float:
    for t in thresholds:
        if t.op == "else":
            return t.weight
        if _OPS[t.op](value, t.value):
            return t.weight
    return 0.0  # unreachable if validate_spec enforced an 'else' catch-all


class _Veto(Exception):
    def __init__(self, term_name: str):
        self.term_name = term_name


def _score_term(term: EvidenceTerm, feature_row: dict, evidence_strength: dict) -> float:
    if term.feature not in feature_row or feature_row[term.feature] is None:
        if term.missing == "veto":
            raise _Veto(term.name)
        if term.missing == "skip":
            return 0.0
        if term.missing.startswith("uncertainty:"):
            penalty = float(term.missing.split(":", 1)[1])
            return penalty
        return 0.0

    weight = _threshold_weight(term.thresholds, feature_row[term.feature])
    evidence_strength[term.name] = weight
    return weight


def evaluate(spec: Spec, feature_row: dict) -> dict:
    """Evaluate `spec` against `feature_row` (a flat dict of feature name ->
    value, as produced by market/heartbeat.py's compute_replayable_fields
    for one asset). Returns a wait or enter decision."""
    evidence_strength: dict = {}
    try:
        total = sum(_score_term(t, feature_row, evidence_strength) for t in spec.evidence)
        total += sum(_score_term(t, feature_row, evidence_strength) for t in spec.secondary_evidence)
    except _Veto as veto:
        return {
            "action": "wait",
            "asset": None,
            "direction": None,
            "confidence": 0.0,
            "evidence_strength": evidence_strength,
            "reason": f"required evidence '{veto.term_name}' missing from feature row (veto)",
        }

    confidence = max(0.0, min(1.0, total))

    if confidence < spec.scale_threshold:
        return {
            "action": "wait",
            "asset": None,
            "direction": None,
            "confidence": confidence,
            "evidence_strength": evidence_strength,
            "reason": f"confidence {confidence:.2f} below scale_threshold {spec.scale_threshold}",
        }

    scaled = confidence < spec.confidence_threshold
    return {
        "action": "enter",
        "asset": None,  # filled in by the caller, which knows which asset this row is for
        "direction": spec.direction,
        "confidence": confidence,
        "evidence_strength": evidence_strength,
        "reason": (
            f"scaled entry: confidence {confidence:.2f} between scale_threshold "
            f"{spec.scale_threshold} and confidence_threshold {spec.confidence_threshold}"
            if scaled else
            f"full-size entry: confidence {confidence:.2f} >= confidence_threshold {spec.confidence_threshold}"
        ),
    }
