"""execution/sizing.py — the ONE confidence-based position-sizing formula.

Shared by the live decision loop (agents/decision_loop.py) and the backtest
engine (backtest/engine.py) so live and backtest cannot size differently
(R4 AC#4: "no third state").  The formula implements the rule every seed
thesis states:

  confidence >= confidence_threshold  → full base size
  scale_threshold <= confidence < confidence_threshold
                                      → base × (0.5 + 0.5·s), where
                                        s = (conf − scale) / (conf_t − scale)
  confidence < scale_threshold        → full base size — sizing never
                                        silently vetoes an entry the
                                        strategy already decided to take
                                        (matching the engine's else-branch;
                                        the interpreter/LLM own the entry
                                        decision, this function only sizes it)

Pure function, no I/O.
"""
from __future__ import annotations

DEFAULT_CONFIDENCE_THRESHOLD = 0.70
DEFAULT_SCALE_THRESHOLD = 0.50


def scale_position_size(
    base_size_pct: float,
    confidence: float | None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    scale_threshold: float = DEFAULT_SCALE_THRESHOLD,
) -> float:
    """Return the position size to execute for a given confidence.

    ``confidence=None`` (a decision that carries no confidence) sizes at
    full base — never invent a discount from missing data.
    """
    if confidence is None:
        return base_size_pct
    if confidence >= confidence_threshold:
        return base_size_pct
    if confidence >= scale_threshold:
        span = confidence_threshold - scale_threshold
        if span <= 0:
            return base_size_pct
        s = (confidence - scale_threshold) / span
        return base_size_pct * (0.5 + 0.5 * s)
    return base_size_pct
