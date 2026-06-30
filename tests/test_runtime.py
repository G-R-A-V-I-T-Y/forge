import pytest
from store.db import insert_agent, insert_account_snapshot
from agents.runtime import AgentRuntime
from market.provider import MarketProvider
from execution.paper_bridge import PaperBridge


@pytest.mark.asyncio
async def test_tick_swallows_raising_llm(conn):
    """tick must not raise even when llm_fn raises."""
    insert_agent(conn, "test", "test", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "test", "paper", 50000.0, 50000.0)

    def bad_llm(system_prompt, decision_prompt):
        raise RuntimeError("LLM exploded")

    config = {
        "universe": ["SOL-PERP"],
        "data_source": "stub",
        "desk": {
            "starting_balance": 50000.0,
            "max_leverage": 10,
            "max_position_size_pct": 0.20,
            "max_concurrent_positions": 3,
        },
    }

    provider = MarketProvider(config)
    async with provider:
        runtime = AgentRuntime(
            agent_id="test",
            thesis_path="agents/theses/jade_hawk_v1.md",
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=bad_llm,
            bridge_factory=lambda agent_id, conn, provider: PaperBridge(
                agent_id=agent_id, conn=conn, provider=provider
            ),
        )
        await runtime.tick()  # must not raise
