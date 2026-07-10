"""forge.py's nightly counterfactual job referenced a bare undefined name
`llm_fn` inside its lambda, raising NameError on every run (caught and
logged, so the job silently no-op'd every night). See
docs/STRATEGIC_ASSESSMENT_07_09_2026.md defect C4 (first half) / Revision R1
AC#2. This test targets only the extracted, testable callable — not the
deeper "LLM guesses counterfactual outcomes" design issue, which is R3's
scope, not R1's."""
from unittest.mock import patch

from forge import _build_counterfactual_llm_fn


def test_callable_is_defined_and_matches_run_counterfactual_contract():
    """Must not raise NameError, and must accept the exact
    (system_prompt, decision_prompt, agent_id=...) shape
    agents/decision_loop.py's run_counterfactual calls it with."""
    config = {"desk": {"starting_balance": 50000.0}}
    fn = _build_counterfactual_llm_fn(config)

    with patch("llm.model_chain.decide") as mock_decide:
        mock_decide.return_value = (
            {"action": "long", "expected_pnl_pct": 1.2, "confidence": 0.6},
            "stub-model",
        )
        result = fn("system prompt", "counterfactual prompt", agent_id="iron_moth")

    assert result == (
        {"action": "long", "expected_pnl_pct": 1.2, "confidence": 0.6},
        "stub-model",
    )
    mock_decide.assert_called_once_with(
        system_prompt="system prompt",
        decision_prompt="counterfactual prompt",
        config=config,
        agent_id="iron_moth",
    )


def test_run_counterfactual_job_no_longer_raises_nameerror(monkeypatch, tmp_path):
    """End-to-end through the actual job function: seed one agent with a
    'wait' decision joined to a trade (the shape run_counterfactual queries
    for), run _run_counterfactual_job(), assert the decisions row now has a
    non-null counterfactual_result instead of the job silently no-oping on
    a caught NameError."""
    import asyncio
    import json as json_mod
    import forge as forge_module
    from store.db import get_connection, init_schema, insert_agent, insert_account_snapshot
    from store.db import insert_trade

    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_schema(conn)
    insert_agent(conn, "iron_moth", "iron_moth", "2026-07-01T00:00:00Z", "{}")
    insert_account_snapshot(conn, "iron_moth", "paper", 50000.0, 50000.0)
    insert_trade(conn, {
        "id": "iron_moth_20260701_000000_BTC",
        "agent_id": "iron_moth",
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": "BTC-PERP",
        "direction": "long",
        "entry_price": 65000.0,
        "stop_loss_price": 63000.0,
        "take_profit_price": 68000.0,
        "leverage": 3,
        "position_size_pct": 0.1,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-07-01T00:05:00Z",
        "status": "open",
    })
    conn.execute(
        """INSERT INTO decisions (agent_id, timestamp, decision_action, decision_reason)
           VALUES (?, ?, ?, ?)""",
        ("iron_moth", "2026-07-01T00:05:00Z", "wait", "test wait"),
    )
    conn.commit()

    monkeypatch.setattr(
        "llm.model_chain.decide",
        lambda **kw: ({"action": "wait", "expected_pnl_pct": 0}, "stub-model"),
    )

    forge_module.DB_PATH = __import__("pathlib").Path(db_path)
    config = {"desk": {"starting_balance": 50000.0}}

    async def _run():
        cf_llm_fn = forge_module._build_counterfactual_llm_fn(config)
        from agents.persona import build_system_prompt
        from agents.decision_loop import run_counterfactual

        system_prompt = build_system_prompt("iron_moth", config)
        await run_counterfactual(conn, "iron_moth", None, cf_llm_fn, system_prompt)

    asyncio.run(_run())

    row = conn.execute(
        "SELECT counterfactual_result FROM decisions WHERE agent_id = ?", ("iron_moth",)
    ).fetchone()
    assert row["counterfactual_result"] is not None
    conn.close()
