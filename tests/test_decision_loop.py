import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.decision_loop import run_decision
from agents.prompt_builder import build_decision_prompt
from execution.paper_bridge import PaperBridge
from llm.stub import decide
from market.heartbeat import write_heartbeat
from market.provider import MarketProvider
from store.db import insert_account_snapshot, insert_agent

AGENT_ID = "jade_hawk"
THESIS = (
    "Funding rate mean reversion: persistent negative funding signals short squeeze."
)


def _config(heartbeat_path: str) -> dict:
    return {
        "universe": ["SOL-PERP"],
        "data_source": "stub",
        "desk": {
            "starting_balance": 50000.0,
            "max_leverage": 10,
            "max_position_size_pct": 0.20,
            "max_concurrent_positions": 3,
            "drawdown_kill_pct": 0.15,
            "heartbeat_path": heartbeat_path,
            "heartbeat_interval_seconds": 300,
        },
    }


def _fresh_heartbeat_packet(timestamp: str | None = None) -> dict:
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "timestamp": ts,
        "assets": {
            "SOL-PERP": {
                "price": 145.20,
                "return_5m": 0.001,
                "return_24h": 0.05,
                "funding": -0.0042,
                "rsi": 61.3,
                "depth_imbalance": 0.12,
                "oi_zscore": 0.4,
            }
        },
        "cross_asset": {
            "market_breadth": 0.6,
            "leader": "SOL-PERP",
            "laggard": "SOL-PERP",
            "sector_strength": {"L1": 0.05},
        },
        "regime": {
            "regime_tag": "range_high_vol",
            "risk_on_score": 0.55,
            "trend_score": 0.2,
            "crypto_fear_index": 42,
        },
    }


def _bridge_factory(config):
    def bridge_factory(agent_id, conn, provider):
        return PaperBridge(
            agent_id=agent_id, conn=conn, provider=provider, config=config
        )

    return bridge_factory


STUB_MODEL_LABEL = "Test Stub Model"


def _stub_llm_fn(system_prompt, decision_prompt, **kwargs):
    """llm_fn now returns (decision_dict, model_display_name) — see
    llm/model_chain.py's decide(). Wraps llm.stub.decide() for tests that
    only care about the decision shape, not the fallback chain itself."""
    return decide(system_prompt, decision_prompt), STUB_MODEL_LABEL


@pytest.mark.asyncio
async def test_decision_loop_enter_creates_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_stub_llm_fn,
            bridge_factory=_bridge_factory(config),
        )

    assert result["action"] == "enter"
    from store.db import get_trades

    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1
    # Fingerprint's categorical regime tag came from the heartbeat packet.
    row = conn.execute(
        "SELECT regime FROM trades WHERE id = ?", (trades[0]["id"],)
    ).fetchone()
    assert row["regime"] == "range_high_vol"

    # The consolidated trade-thumbprint (portfolio + cross_asset + regime +
    # full per-asset heartbeat fields) was captured onto the trade row.
    from store.query import get_trade

    full = get_trade(conn, trades[0]["id"], decode_ohlcv=True)
    mc = full["market_context_json"]
    assert mc is not None
    assert set(mc.keys()) == {"portfolio", "cross_asset", "regime", "asset"}
    assert mc["portfolio"]["cash"] == 50000.0
    assert mc["cross_asset"]["leader"] == "SOL-PERP"
    assert mc["regime"]["regime_tag"] == "range_high_vol"
    assert mc["asset"]["price"] == 145.20
    assert mc["asset"]["funding"] == -0.0042

    # model_used was recorded on the trade row and last_model_used on the
    # agent row from the (decision, model_used) tuple returned by llm_fn.
    assert full["model_used"] == STUB_MODEL_LABEL
    from store.db import get_agent

    assert get_agent(conn, AGENT_ID)["last_model_used"] == STUB_MODEL_LABEL


@pytest.mark.asyncio
async def test_decision_loop_risk_block_does_not_create_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def bad_llm(sys, prompt, **kwargs):
        return {
            "action": "enter",
            "asset": "SOL-PERP",
            "direction": "long",
            "entry_price": 145.20,
            "stop_loss_price": 143.00,
            "take_profit_price": 152.00,
            "leverage": 15,
            "position_size_pct": 0.10,
            "hypothesis": "test",
            "key_conditions_met": [],
            "key_conditions_missing": [],
            "confidence": 0.5,
            "expected_value": "test",
        }, "Test Bad Model"

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=bad_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result["action"] == "risk_blocked"
    from store.db import get_trades

    assert get_trades(conn, AGENT_ID) == []


@pytest.mark.asyncio
async def test_decision_loop_wait_does_not_create_trade(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def wait_llm(sys, prompt, **kwargs):
        return {
            "action": "wait",
            "reason": "no setup fits thesis today",
        }, "Test Wait Model"

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=wait_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result["action"] == "wait"
    from store.db import get_trades

    assert get_trades(conn, AGENT_ID) == []

    # "wait" cycles still update last_model_used — the captain wants "most
    # recently used model", not "model used for the last trade".
    from store.db import get_agent

    assert get_agent(conn, AGENT_ID)["last_model_used"] == "Test Wait Model"


@pytest.mark.asyncio
async def test_decision_loop_error_action_propagates_and_records_no_model_available(
    conn, tmp_path
):
    """When llm_fn (llm/model_chain.py's decide()) exhausts every tier, it
    returns ({"action": "error", "reason": "no model available"}, None).
    run_decision() must surface that as an explicit error result (not a
    generic "wait") and record the literal "no model available" sentinel
    on the agent row, distinct from NULL (no cycle run yet)."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def no_model_llm(sys, prompt, **kwargs):
        return {"action": "error", "reason": "no model available"}, None

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=no_model_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result == {"action": "error", "detail": "no model available"}
    from store.db import get_agent, get_trades

    assert get_agent(conn, AGENT_ID)["last_model_used"] == "no model available"
    assert get_trades(conn, AGENT_ID) == []


@pytest.mark.asyncio
async def test_decision_loop_missing_heartbeat_returns_wait(conn, tmp_path):
    """No heartbeat file at all -> immediate wait, no live-API fallback."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    heartbeat_path = str(tmp_path / "does_not_exist.json")
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_stub_llm_fn,
            bridge_factory=_bridge_factory(config),
        )

    assert result == {"action": "wait", "detail": "heartbeat unavailable or stale"}
    from store.db import get_trades

    assert get_trades(conn, AGENT_ID) == []


@pytest.mark.asyncio
async def test_decision_loop_stale_heartbeat_returns_wait(conn, tmp_path):
    """Heartbeat file exists but its timestamp is past 2x the interval -> wait."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet(timestamp=stale_ts))
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id=AGENT_ID,
            thesis_text=THESIS,
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_stub_llm_fn,
            bridge_factory=_bridge_factory(config),
        )

    assert result == {"action": "wait", "detail": "heartbeat unavailable or stale"}


@pytest.mark.asyncio
async def test_decision_prompt_contains_heartbeat_sourced_data_and_cadence_notice(
    conn, tmp_path
):
    """End-to-end: heartbeat-sourced fields actually reach the decision
    prompt text built by build_decision_prompt (not just the reader
    function in isolation), and the 5-minute cadence language is present."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    packet = _fresh_heartbeat_packet()
    prompt = await build_decision_prompt(
        AGENT_ID,
        THESIS,
        packet,
        conn,
        provider=None,
        starting_balance=50000.0,
        universe=["SOL-PERP"],
    )

    # Heartbeat-sourced per-asset field values reach the prompt text.
    assert "SOL-PERP" in prompt
    assert "145.2" in prompt  # price
    assert "range_high_vol" in prompt  # regime_tag
    assert "SOL-PERP" in prompt.split("Leader:")[1][:20]  # cross_asset leader

    # Hard cadence-awareness requirement from the captain.
    assert "every 5 minutes" in prompt
    assert "Do not assume intraday granularity finer than 5 minutes" in prompt


@pytest.mark.asyncio
async def test_decision_loop_wait_logs_confidence_and_evidence_to_ledger(conn, tmp_path):
    """The selection-bias fix: a 'wait' decision's confidence/evidence must
    reach both the decisions table and the git-tracked ledger, not just a
    bare reason string."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def wait_llm(sys, prompt, **kwargs):
        return {
            "action": "wait",
            "reason": "days_to_event too far",
            "confidence": 0.35,
            "evidence_strength": {"unlock_size": 0.0, "days_to_event": 0.3},
        }, "Test Wait Model"

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    ledger_dir = tmp_path / "ledger"
    import store.ledger as ledger_module

    original_dir = ledger_module.LEDGER_DIR
    ledger_module.LEDGER_DIR = str(ledger_dir)
    try:
        provider = MarketProvider(config)
        async with provider:
            result = await run_decision(
                agent_id=AGENT_ID,
                thesis_text=THESIS,
                config=config,
                conn=conn,
                provider=provider,
                llm_fn=wait_llm,
                bridge_factory=_bridge_factory(config),
            )
    finally:
        ledger_module.LEDGER_DIR = original_dir

    assert result["action"] == "wait"

    row = conn.execute(
        "SELECT decision_action, decision_reason FROM decisions WHERE agent_id = ?", (AGENT_ID,)
    ).fetchone()
    assert row["decision_action"] == "wait"

    from datetime import datetime, timezone
    month_file = ledger_dir / "decisions" / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
    assert month_file.exists()
    lines = month_file.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["agent"] == AGENT_ID
    assert record["action"] == "wait"
    assert record["confidence"] == 0.35
    assert record["evidence_strength"] == {"unlock_size": 0.0, "days_to_event": 0.3}
    assert record["model"] == "Test Wait Model"


# ---------------------------------------------------------------------------
# M8: Compiled agent tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compiled_agent_uses_spec(conn, tmp_path, monkeypatch):
    """A compiled agent calls the interpreter instead of the LLM.

    The spec's evidence term fires on favorable funding → enter decision
    with model_used="compiled/v1".
    """
    from backtest.dsl import EvidenceTerm, Spec, Threshold
    from store.specs import deploy_spec, get_active_spec

    monkeypatch.setattr("store.specs.SPECS_DIR", Path(str(tmp_path / "specs")))

    insert_agent(
        conn,
        "sage_turtle",
        "sage_turtle",
        "2026-07-09T00:00:00Z",
        json.dumps({"compiled": True}),
    )
    insert_account_snapshot(conn, "sage_turtle", "paper", 50000.0, 50000.0)

    spec = Spec(
        agent_id="sage_turtle",
        spec_version=1,
        thesis_version=1,
        universe_include=["SOL-PERP"],
        regime_exclude=[],
        direction="long",
        confidence_threshold=0.5,
        scale_threshold=0.3,
        evidence=[
            EvidenceTerm(
                name="funding_dislocation",
                feature="funding",
                thresholds=[
                    Threshold(op="<", weight=0.8, value=-0.001),
                    Threshold(op="else", weight=0.0),
                ],
                missing="skip",
            ),
        ],
        secondary_evidence=[],
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        max_hold_hours=72,
        leverage=3,
        position_size_pct=0.10,
    )
    deploy_spec(conn, "sage_turtle", spec)

    deployed = get_active_spec(conn, "sage_turtle")
    assert deployed is not None, "spec should be active after deploy"

    heartbeat_path = str(tmp_path / "heartbeat.json")
    packet = _fresh_heartbeat_packet()
    packet["assets"]["SOL-PERP"]["funding"] = -0.005
    write_heartbeat(heartbeat_path, packet)
    config = _config(heartbeat_path)

    called_llm = False

    def sentinel_llm(sp, dp, **kw):
        nonlocal called_llm
        called_llm = True
        return ({"action": "wait", "reason": "should not be called"}, None)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id="sage_turtle",
            thesis_text="test thesis",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=sentinel_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result["action"] == "enter", f"expected enter, got {result}"
    assert not called_llm, "compiled agent must not call the LLM"

    from store.db import get_agent

    agent = get_agent(conn, "sage_turtle")
    assert agent["last_model_used"] == "compiled/v1"

    from store.db import get_trades

    trades = get_trades(conn, "sage_turtle")
    assert len(trades) >= 1

    # The decision's model field goes into the ledger via log_decision.
    import store.ledger as ledger_module

    month_file = (
        Path(str(tmp_path / "ledger"))
        / "decisions"
        / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
    )
    if month_file.exists():
        lines = month_file.read_text(encoding="utf-8").strip().splitlines()
        record = json.loads(lines[-1])
        assert record["agent"] == "sage_turtle"
        assert record["model"] == "compiled/v1"


@pytest.mark.asyncio
async def test_control_arm_uses_llm(conn, tmp_path, monkeypatch):
    """A pure-LLM agent still calls the LLM even when a spec is present.

    The compiled check only fires when config_json has compiled=True.
    Without that flag, the regular LLM path runs regardless of any
    deployed spec.
    """
    from backtest.dsl import EvidenceTerm, Spec, Threshold
    from store.specs import deploy_spec

    monkeypatch.setattr("store.specs.SPECS_DIR", Path(str(tmp_path / "specs2")))

    # Insert agent WITHOUT compiled: true
    insert_agent(
        conn,
        "silver_basin",
        "silver_basin",
        "2026-07-09T00:00:00Z",
        json.dumps({"wake_interval": 300}),
    )
    insert_account_snapshot(conn, "silver_basin", "paper", 50000.0, 50000.0)

    # Deploy a spec (should be ignored by non-compiled agents)
    spec = Spec(
        agent_id="silver_basin",
        spec_version=1,
        thesis_version=1,
        universe_include=["SOL-PERP"],
        regime_exclude=[],
        direction="long",
        confidence_threshold=0.5,
        scale_threshold=0.3,
        evidence=[
            EvidenceTerm(
                name="funding_dislocation",
                feature="funding",
                thresholds=[
                    Threshold(op="<", weight=0.8, value=-0.001),
                    Threshold(op="else", weight=0.0),
                ],
                missing="skip",
            ),
        ],
        secondary_evidence=[],
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        max_hold_hours=72,
        leverage=3,
        position_size_pct=0.10,
    )
    deploy_spec(conn, "silver_basin", spec)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id="silver_basin",
            thesis_text="Test thesis",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=_stub_llm_fn,
            bridge_factory=_bridge_factory(config),
        )

    # Should go through normal LLM path, not compiled
    assert result["action"] == "enter", f"expected enter, got {result}"

    from store.db import get_agent

    agent = get_agent(conn, "silver_basin")
    assert agent["last_model_used"] == STUB_MODEL_LABEL


@pytest.mark.asyncio
async def test_compiled_no_spec_falls_back_to_wait(conn, tmp_path):
    """A compiled agent with no active spec logs a wait decision."""
    insert_agent(
        conn,
        "sage_turtle",
        "sage_turtle",
        "2026-07-09T00:00:00Z",
        json.dumps({"compiled": True}),
    )
    insert_account_snapshot(conn, "sage_turtle", "paper", 50000.0, 50000.0)

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    called_llm = False

    def sentinel_llm(sp, dp, **kw):
        nonlocal called_llm
        called_llm = True
        return ({"action": "wait", "reason": "should not be called"}, None)

    provider = MarketProvider(config)
    async with provider:
        result = await run_decision(
            agent_id="sage_turtle",
            thesis_text="test thesis",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=sentinel_llm,
            bridge_factory=_bridge_factory(config),
        )

    assert result == {"action": "wait", "detail": "compiled: no active spec deployed"}
    assert not called_llm, "compiled agent must not call the LLM"

    row = conn.execute(
        "SELECT decision_action, decision_reason FROM decisions WHERE agent_id = ?",
        ("sage_turtle",),
    ).fetchone()
    assert row is not None
    assert row["decision_action"] == "wait"
    assert row["decision_reason"] == "compiled: no active spec deployed"
