"""backtest/validator.py -- semantic validation for a loaded Spec.

Structural validation (required fields, types) happens in dsl.py's
load_spec via YAML/dataclass construction, which raises on malformed input.
This module checks the SEMANTIC rules a structurally-valid spec can still
violate: referencing a feature the interpreter can never compute, an
internally-inconsistent threshold list, or position parameters that would
fail risk/gate.py's live checks (rejected here at compile time instead of
discovered at backtest or live-trading time).
"""
from __future__ import annotations

from backtest.dsl import EvidenceTerm, Spec
from market.features import FEATURE_REGISTRY

# Fields compute_replayable_fields() always produces (market/heartbeat.py),
# excluding the live-only fields that function deliberately never computes.
# Kept in sync by hand with market/heartbeat.py's PER_ASSET_FIELDS minus the
# live-only set _compute_live_only_fields() returns -- both lists are small
# and stable, and a mismatch here is caught immediately by
# test_unknown_feature_name_is_rejected-style tests against real specs.
REPLAYABLE_FEATURES = {
    "price", "return_5m", "return_30m", "return_4h", "return_24h", "volume",
    "open_interest", "funding", "atr", "realized_vol", "rsi",
    "ema20", "ema50", "ema200", "vwap_distance", "volume_zscore",
    "funding_zscore", "oi_zscore", "oi_drawdown_pct", "liquidation_cascade_flag",
    "liq_total_usd", "liq_long_usd", "liq_short_usd",
} | set(FEATURE_REGISTRY.keys())

VALID_OPS = {">", ">=", "<", "<=", "between", "==", "else"}


def _validate_evidence_term(term: EvidenceTerm, label: str) -> list[str]:
    errors = []
    if term.feature not in REPLAYABLE_FEATURES:
        errors.append(
            f"{label} '{term.name}': feature '{term.feature}' is not in the "
            f"replayable feature vocabulary (not computable from ledger data)"
        )
    if not term.thresholds or term.thresholds[-1].op != "else":
        errors.append(f"{label} '{term.name}': thresholds must end with an 'else' catch-all")
    for t in term.thresholds:
        if t.op not in VALID_OPS:
            errors.append(f"{label} '{term.name}': unknown threshold op '{t.op}'")
        if t.op == "between" and (not isinstance(t.value, list) or len(t.value) != 2):
            errors.append(f"{label} '{term.name}': 'between' requires value: [lo, hi]")
    if term.missing not in ("veto", "skip") and not term.missing.startswith("uncertainty:"):
        errors.append(
            f"{label} '{term.name}': missing rule '{term.missing}' must be "
            f"'veto', 'skip', or 'uncertainty:-N'"
        )
    return errors


def validate_spec(spec: Spec, config: dict) -> list[str]:
    """Returns a list of human-readable error strings; empty = valid."""
    errors: list[str] = []

    if spec.direction not in ("long", "short", "signal_determined"):
        errors.append(f"direction '{spec.direction}' must be 'long', 'short', or 'signal_determined'")

    if spec.scale_threshold > spec.confidence_threshold:
        errors.append(
            f"scale_threshold ({spec.scale_threshold}) must not exceed "
            f"confidence_threshold ({spec.confidence_threshold})"
        )

    if not spec.evidence:
        errors.append("entry.evidence must have at least one term")

    for term in spec.evidence:
        errors.extend(_validate_evidence_term(term, "evidence"))
    for term in spec.secondary_evidence:
        errors.extend(_validate_evidence_term(term, "secondary_evidence"))

    if spec.leverage > config["max_leverage"]:
        errors.append(f"leverage {spec.leverage}x exceeds desk cap {config['max_leverage']}x")
    if spec.position_size_pct > config["max_position_size_pct"]:
        errors.append(
            f"position_size_pct {spec.position_size_pct:.0%} exceeds desk cap "
            f"{config['max_position_size_pct']:.0%}"
        )
    notional_exposure = spec.position_size_pct * spec.leverage
    if notional_exposure > 2.0:  # matches risk/gate.py's MAX_NOTIONAL_EXPOSURE
        errors.append(
            f"notional exposure {notional_exposure:.2f} (size × leverage) exceeds max 2.00"
        )

    if spec.stop_loss_pct <= 0:
        errors.append("stop_loss_pct must be positive")
    if spec.take_profit_pct <= 0:
        errors.append("take_profit_pct must be positive")

    return errors
