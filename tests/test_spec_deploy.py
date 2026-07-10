"""tests/test_spec_deploy.py -- M8 spec deploy pipeline hot-reload behaviour.

Covers the missing M8 test-table row: "Writing a new spec version to the DB
causes the agent's next heartbeat to use it without a restart." store/specs.py's
get_active_spec() always queries SQLite (and re-reads the YAML file from disk)
fresh on every call rather than caching anything in-process, so a newly
deployed spec version is visible immediately -- this test demonstrates exactly
that, plus the supporting hot_reload_spec() detection helper.
"""
import pytest

from backtest.dsl import load_spec
from store.db import insert_agent
from store.specs import deploy_spec, get_active_spec, get_spec_history, hot_reload_spec

CONFIG = {"max_leverage": 10, "max_position_size_pct": 0.20, "max_concurrent_positions": 3}


def _spec_yaml(agent_id: str, spec_version: int, leverage: int = 3) -> str:
    return f"""
agent_id: {agent_id}
spec_version: {spec_version}
thesis_version: 1

universe:
  include: [SOL-PERP, ETH-PERP]

regime_filter:
  exclude: [crisis]

entry:
  direction: long
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: momentum_rank
      feature: momentum_acceleration
      thresholds:
        - {{op: ">=", value: 0.5, weight: 0.7}}
        - {{op: "else", weight: 0.0}}
      missing: veto
  secondary_evidence: []

exit:
  stop_loss_pct: 0.025
  take_profit_pct: 0.05
  max_hold_hours: 12

position:
  leverage: {leverage}
  position_size_pct: 0.10
"""


@pytest.fixture(autouse=True)
def _redirect_specs_dir(tmp_path, monkeypatch):
    """Never let a test write into the real agents/specs/ directory."""
    import store.specs as specs_module

    monkeypatch.setattr(specs_module, "SPECS_DIR", tmp_path / "specs")


def _seed_agent(conn, agent_id: str) -> None:
    insert_agent(conn, agent_id, agent_id, "2026-01-01T00:00:00Z", "{}")


def test_spec_hot_reload(conn):
    """Deploying spec v1 then v2 for the same agent makes get_active_spec()
    return v2 immediately -- no restart, no caching, just a fresh DB read."""
    _seed_agent(conn, "iron_moth")

    spec_v1 = load_spec_from_text(_spec_yaml("iron_moth", 1, leverage=3))
    deploy_spec(conn, "iron_moth", spec_v1, config=CONFIG)

    active = get_active_spec(conn, "iron_moth")
    assert active is not None
    assert active.spec_version == 1
    assert active.leverage == 3

    # A newer version lands in the DB (e.g. from the M8 spec-deploy pipeline
    # reacting to a thesis rewrite) -- no process restart happens here.
    spec_v2 = load_spec_from_text(_spec_yaml("iron_moth", 2, leverage=5))
    deploy_spec(conn, "iron_moth", spec_v2, config=CONFIG)

    active_after = get_active_spec(conn, "iron_moth")
    assert active_after is not None
    assert active_after.spec_version == 2
    assert active_after.leverage == 5

    # The old version is marked inactive; history retains both.
    history = get_spec_history(conn, "iron_moth")
    versions_by_status = {row["spec_version"]: row["status"] for row in history}
    assert versions_by_status == {1: "inactive", 2: "active"}


def test_hot_reload_spec_detects_newer_version(conn):
    """hot_reload_spec() flags True once a newer active version exists in the
    DB than the agent's current active_spec_version, and False once the agent
    record has caught up."""
    _seed_agent(conn, "silver_basin")

    spec_v1 = load_spec_from_text(_spec_yaml("silver_basin", 1))
    deploy_spec(conn, "silver_basin", spec_v1, config=CONFIG)

    # Freshly deployed v1 matches the agent's active_spec_version (deploy_spec
    # updates it) -- nothing to reload yet.
    assert hot_reload_spec(conn, "silver_basin") is False

    spec_v2 = load_spec_from_text(_spec_yaml("silver_basin", 2))
    deploy_spec(conn, "silver_basin", spec_v2, config=CONFIG)

    # deploy_spec() already bumped active_spec_version to 2 as part of the
    # deploy, so there is nothing pending either -- confirm both states.
    assert hot_reload_spec(conn, "silver_basin") is False

    # Simulate an agent runtime that hasn't picked up v2 yet (e.g. still
    # holding a cached Spec object from before this deploy): roll its
    # recorded active_spec_version back and confirm hot_reload_spec now
    # reports a pending reload.
    conn.execute(
        "UPDATE agents SET active_spec_version = 1 WHERE id = ?", ("silver_basin",)
    )
    conn.commit()
    assert hot_reload_spec(conn, "silver_basin") is True

    # And get_active_spec() -- queried fresh -- already reflects v2 regardless
    # of what the agent row's bookkeeping column says.
    active = get_active_spec(conn, "silver_basin")
    assert active.spec_version == 2


def load_spec_from_text(yaml_text: str):
    """Helper: load_spec() takes a filesystem path, so round-trip through a
    temp file for tests that build spec YAML inline."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "spec.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        return load_spec(str(path))
