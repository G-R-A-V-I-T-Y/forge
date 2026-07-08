from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.interpreter import evaluate


def _spec(evidence, confidence_threshold=0.70, scale_threshold=0.50, direction="short"):
    return Spec(
        agent_id="test", spec_version=1, thesis_version=1,
        universe_include=["FET-PERP"], regime_exclude=[],
        direction=direction, confidence_threshold=confidence_threshold,
        scale_threshold=scale_threshold, evidence=evidence, secondary_evidence=[],
        stop_loss_pct=0.03, take_profit_pct=0.06, max_hold_hours=24,
        leverage=3, position_size_pct=0.10,
    )


def test_high_confidence_enters():
    spec = _spec([
        EvidenceTerm(
            name="unlock_size", feature="unlock_size_pct_float",
            thresholds=[Threshold(op=">=", value=0.03, weight=0.8), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ])
    result = evaluate(spec, {"unlock_size_pct_float": 0.05})
    assert result["action"] == "enter"
    assert result["direction"] == "short"
    assert result["confidence"] == 0.8
    assert result["evidence_strength"] == {"unlock_size": 0.8}


def test_low_confidence_waits():
    spec = _spec([
        EvidenceTerm(
            name="unlock_size", feature="unlock_size_pct_float",
            thresholds=[Threshold(op=">=", value=0.03, weight=0.8), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ])
    result = evaluate(spec, {"unlock_size_pct_float": 0.001})  # hits the else -> weight 0.0
    assert result["action"] == "wait"
    assert result["confidence"] == 0.0


def test_missing_feature_veto_forces_wait():
    spec = _spec([
        EvidenceTerm(
            name="unlock_size", feature="unlock_size_pct_float",
            thresholds=[Threshold(op=">=", value=0.03, weight=0.8), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ])
    result = evaluate(spec, {})  # feature entirely absent from the row
    assert result["action"] == "wait"
    assert "unlock_size" in result["reason"]


def test_missing_feature_skip_contributes_zero_no_veto():
    spec = _spec([
        EvidenceTerm(
            name="a", feature="funding_zscore",
            thresholds=[Threshold(op=">", value=2.0, weight=0.6), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
        EvidenceTerm(
            name="b", feature="missing_optional_feature",
            thresholds=[Threshold(op=">", value=0.0, weight=0.3), Threshold(op="else", weight=0.0)],
            missing="skip",
        ),
    ], confidence_threshold=0.5, scale_threshold=0.3)
    result = evaluate(spec, {"funding_zscore": 3.0})  # only 'a' present; 'b' skips, no veto
    assert result["action"] == "enter"
    assert result["confidence"] == 0.6
    assert "b" not in result["evidence_strength"]


def test_between_operator():
    spec = _spec([
        EvidenceTerm(
            name="days_to_event", feature="days_to_next_unlock",
            thresholds=[
                Threshold(op="between", value=[1, 4], weight=0.6),
                Threshold(op="between", value=[4, 10], weight=0.3),
                Threshold(op="else", weight=0.0),
            ],
            missing="veto",
        ),
    ], confidence_threshold=0.5, scale_threshold=0.3)
    result = evaluate(spec, {"days_to_next_unlock": 2})
    assert result["confidence"] == 0.6


def test_scaled_size_between_scale_and_confidence_threshold():
    spec = _spec([
        EvidenceTerm(
            name="a", feature="funding_zscore",
            thresholds=[Threshold(op=">", value=1.0, weight=0.55), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ], confidence_threshold=0.70, scale_threshold=0.50)
    result = evaluate(spec, {"funding_zscore": 1.5})
    assert result["action"] == "enter"
    assert result["confidence"] == 0.55
    assert result["reason"]  # non-empty, notes this is a scaled (not full) entry
