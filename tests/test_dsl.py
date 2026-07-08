import pytest

from backtest.dsl import load_spec, Spec


SAMPLE_SPEC_YAML = """
agent_id: sage_turtle
spec_version: 1
thesis_version: 1

universe:
  include: [FET-PERP, TAO-PERP]

regime_filter:
  exclude: [crisis]

entry:
  direction: short
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: unlock_size_vs_float
      feature: unlock_size_pct_float
      thresholds:
        - {op: ">=", value: 0.03, weight: 0.7}
        - {op: ">=", value: 0.015, weight: 0.5}
        - {op: "else", weight: 0.0}
      missing: veto
  secondary_evidence: []

exit:
  stop_loss_pct: 0.03
  take_profit_pct: 0.06
  max_hold_hours: 24

position:
  leverage: 3
  position_size_pct: 0.10
"""


def test_load_spec_parses_all_fields(tmp_path):
    path = tmp_path / "sage_turtle_v1.yaml"
    path.write_text(SAMPLE_SPEC_YAML, encoding="utf-8")

    spec = load_spec(str(path))

    assert isinstance(spec, Spec)
    assert spec.agent_id == "sage_turtle"
    assert spec.spec_version == 1
    assert spec.universe_include == ["FET-PERP", "TAO-PERP"]
    assert spec.regime_exclude == ["crisis"]
    assert spec.direction == "short"
    assert spec.confidence_threshold == 0.70
    assert spec.scale_threshold == 0.50
    assert len(spec.evidence) == 1
    assert spec.evidence[0].name == "unlock_size_vs_float"
    assert spec.evidence[0].feature == "unlock_size_pct_float"
    assert spec.evidence[0].missing == "veto"
    assert len(spec.evidence[0].thresholds) == 3
    assert spec.evidence[0].thresholds[0].op == ">="
    assert spec.evidence[0].thresholds[0].value == 0.03
    assert spec.evidence[0].thresholds[0].weight == 0.7
    assert spec.evidence[0].thresholds[-1].op == "else"
    assert spec.stop_loss_pct == 0.03
    assert spec.take_profit_pct == 0.06
    assert spec.max_hold_hours == 24
    assert spec.leverage == 3
    assert spec.position_size_pct == 0.10


def test_load_spec_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_spec(str(tmp_path / "nope.yaml"))
