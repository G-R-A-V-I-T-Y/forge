"""backtest/dsl.py -- the strategy-spec DSL: schema + YAML loader.

An evidence-weighted YAML format matching the shape every thesis already
uses (see docs/superpowers/specs/2026-07-07-strategy-spec-dsl-backtester-design.md
section 2 for the full field reference). Not free code (unsafe, unverifiable)
and not a rigid config (kills expressiveness) -- entry conditions as weighted
evidence terms over the same feature vocabulary market/heartbeat.py's
replayable core produces.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml


@dataclass(frozen=True)
class Threshold:
    op: str  # ">", ">=", "<", "<=", "between", "==", "else"
    weight: float
    value: float | list[float] | None = None


@dataclass(frozen=True)
class EvidenceTerm:
    name: str
    feature: str
    thresholds: list[Threshold]
    missing: str  # "veto" | "skip" | "uncertainty:-0.1" (a skip with a flat penalty)


@dataclass(frozen=True)
class Spec:
    agent_id: str
    spec_version: int
    thesis_version: int
    universe_include: list[str]
    regime_exclude: list[str]
    direction: str  # "long" | "short" | "signal_determined"
    confidence_threshold: float
    scale_threshold: float
    evidence: list[EvidenceTerm]
    secondary_evidence: list[EvidenceTerm]
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_hours: float
    leverage: int
    position_size_pct: float


def _parse_threshold(raw: dict) -> Threshold:
    return Threshold(op=raw["op"], weight=raw["weight"], value=raw.get("value"))


def _parse_evidence_term(raw: dict) -> EvidenceTerm:
    return EvidenceTerm(
        name=raw["name"],
        feature=raw["feature"],
        thresholds=[_parse_threshold(t) for t in raw["thresholds"]],
        missing=raw["missing"],
    )


def load_spec(path: str) -> Spec:
    """Parse a spec YAML file into a Spec. Raises FileNotFoundError if the
    path doesn't exist; raises KeyError with the missing field name if the
    YAML is missing a required key (fail loud -- a malformed spec must never
    silently produce a partially-valid Spec)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    entry = raw["entry"]
    exit_ = raw["exit"]
    position = raw["position"]

    return Spec(
        agent_id=raw["agent_id"],
        spec_version=raw["spec_version"],
        thesis_version=raw["thesis_version"],
        universe_include=raw["universe"]["include"],
        regime_exclude=raw.get("regime_filter", {}).get("exclude", []),
        direction=entry["direction"],
        confidence_threshold=entry["confidence_threshold"],
        scale_threshold=entry["scale_threshold"],
        evidence=[_parse_evidence_term(e) for e in entry["evidence"]],
        secondary_evidence=[_parse_evidence_term(e) for e in entry.get("secondary_evidence", [])],
        stop_loss_pct=exit_["stop_loss_pct"],
        take_profit_pct=exit_["take_profit_pct"],
        max_hold_hours=exit_["max_hold_hours"],
        leverage=position["leverage"],
        position_size_pct=position["position_size_pct"],
    )
