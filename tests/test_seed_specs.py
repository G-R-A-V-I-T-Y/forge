import glob

import pytest
import yaml

from backtest.dsl import load_spec
from backtest.validator import validate_spec

CONFIG = {"max_leverage": 10, "max_position_size_pct": 0.20, "max_concurrent_positions": 3}


@pytest.mark.parametrize("spec_path", sorted(glob.glob("agents/specs/*.yaml")))
def test_seed_spec_is_valid(spec_path):
    spec = load_spec(spec_path)
    errors = validate_spec(spec, CONFIG)
    assert errors == [], f"{spec_path}: {errors}"


def test_four_seed_specs_exist():
    # steel_crane was swapped for jade_hawk: steel_crane's primary evidence
    # (liq_total_usd, missing: veto) can never be non-null since liquidation
    # history is never backfilled -- permanent veto, not a real backtest.
    # jade_hawk (VWAP mean reversion) is candle/funding-driven and can
    # actually be evaluated against real historical data.
    #
    # sage_turtle was added as a 4th, M8-era seed spec (event/unlock
    # positioning): its primary evidence (unlock_size_pct, days_to_event) is
    # backed by the M7b event calendar wired into
    # compute_replayable_fields()/market/features.py, so it too is real and
    # backtestable, not a permanent veto like steel_crane was.
    paths = sorted(glob.glob("agents/specs/*.yaml"))
    agent_ids = {load_spec(p).agent_id for p in paths}
    assert agent_ids == {"silver_basin", "iron_moth", "jade_hawk", "sage_turtle"}
