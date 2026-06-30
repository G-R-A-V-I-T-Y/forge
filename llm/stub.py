"""Stub LLM — returns a hardcoded valid SOL long trade. No network calls."""

_STUB_RESPONSE = {
    "action": "enter",
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
    "hypothesis": (
        "SOL funding has been negative for 3 consecutive 8h periods indicating sustained "
        "short pressure. Long liquidations in the last hour suggest a squeeze setup as "
        "trapped shorts face escalating cost. Price has held the 145 level on two 15m retests."
    ),
    "key_conditions_met": ["persistent_negative_funding", "support_hold_15m"],
    "key_conditions_missing": [],
    "confidence": 0.65,
    "expected_value": "+1.0% EV: 62% win rate × 4.7% TP − 38% × 2.5% SL",
}


def decide(system_prompt: str, decision_prompt: str) -> dict:
    """Returns a hardcoded valid SOL long trade decision.

    Args:
        system_prompt: The system prompt (unused in stub)
        decision_prompt: The decision prompt (unused in stub)

    Returns:
        A dict with a complete trade action structure.
    """
    return dict(_STUB_RESPONSE)
