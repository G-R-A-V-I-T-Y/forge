from backtest.dsl import load_spec
from backtest.validator import validate_spec

VALID_YAML = """
agent_id: test_agent
spec_version: 1
thesis_version: 1
universe: {include: [BTC-PERP]}
regime_filter: {exclude: []}
entry:
  direction: long
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: funding_extreme
      feature: funding_zscore
      thresholds:
        - {op: ">", value: 2.0, weight: 0.7}
        - {op: "else", weight: 0.0}
      missing: veto
  secondary_evidence: []
exit: {stop_loss_pct: 0.02, take_profit_pct: 0.04, max_hold_hours: 8}
position: {leverage: 4, position_size_pct: 0.10}
"""

CONFIG = {"max_leverage": 10, "max_position_size_pct": 0.20, "max_concurrent_positions": 3}


def _spec_from(yaml_text, tmp_path, name="spec.yaml"):
    path = tmp_path / name
    path.write_text(yaml_text, encoding="utf-8")
    return load_spec(str(path))


def test_valid_spec_has_no_errors(tmp_path):
    spec = _spec_from(VALID_YAML, tmp_path)
    assert validate_spec(spec, CONFIG) == []


def test_unknown_feature_name_is_rejected(tmp_path):
    bad = VALID_YAML.replace("funding_zscore", "not_a_real_feature")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("not_a_real_feature" in e for e in errors)


def test_thresholds_must_end_in_else(tmp_path):
    bad = VALID_YAML.replace(
        '        - {op: "else", weight: 0.0}\n', ''
    )
    spec = _spec_from(bad, tmp_path, name="bad.yaml")
    errors = validate_spec(spec, CONFIG)
    assert any("else" in e for e in errors)


def test_scale_threshold_must_not_exceed_confidence_threshold(tmp_path):
    bad = VALID_YAML.replace("scale_threshold: 0.50", "scale_threshold: 0.90")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("scale_threshold" in e for e in errors)


def test_leverage_exceeding_desk_cap_is_rejected(tmp_path):
    bad = VALID_YAML.replace("leverage: 4", "leverage: 15")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("leverage" in e for e in errors)


def test_position_size_exceeding_desk_cap_is_rejected(tmp_path):
    bad = VALID_YAML.replace("position_size_pct: 0.10", "position_size_pct: 0.50")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("position_size_pct" in e for e in errors)
