"""Tests for meta/head_of_desk.py + the /chat WebSocket — M9 criterion 8.

Proposal-named tests:
  - test_daily_briefing_produces_text — briefing is non-empty and references
    at least one agent by name.
  - test_chat_query_returns_stream — a /chat WebSocket query streams
    multiple frames before the done marker and references actual trade data.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from meta.head_of_desk import (
    compose_chat_answer,
    generate_morning_brief,
    get_chat_history,
    save_chat_turn,
)
from store.db import (
    init_schema,
    insert_account_snapshot,
    insert_agent,
    insert_trade,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_schema(c)
    yield c
    c.close()


def _seed_desk(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-01T00:00:00Z", "{}")
    insert_account_snapshot(conn, "jade_hawk", "paper", 50000.0, 50000.0)
    insert_trade(conn, {
        "id": "t1", "agent_id": "jade_hawk", "mode": "paper",
        "asset": "SOL-PERP", "direction": "long",
        "entry_price": 100.0, "exit_price": 105.0,
        "stop_loss_price": 95.0, "take_profit_price": 110.0,
        "leverage": 2, "position_size_pct": 0.1, "notional_usd": 5000.0,
        "entry_timestamp": "2026-07-10T00:00:00Z",
        "exit_timestamp": "2026-07-10T06:00:00Z",
        "pnl_usd": 250.0, "pnl_pct": 0.05, "result": "win",
        "status": "closed", "regime": "range_high_vol",
    })


def test_daily_briefing_produces_text(conn):
    _seed_desk(conn)
    brief = generate_morning_brief(conn)

    assert isinstance(brief["briefing_text"], str)
    assert len(brief["briefing_text"]) > 0
    # References at least one agent by name.
    assert "jade_hawk" in brief["briefing_text"]
    assert "jade_hawk" in brief["agents_covered"]


def test_chat_query_returns_stream(conn):
    """A /chat WS query returns a streaming response (multiple chunk frames
    before the done marker) referencing actual trade data, and both turns
    land in chat_history."""
    _seed_desk(conn)

    from web.app import app

    app.state.conn = conn
    # Multi-line answer so the handler emits several chunk frames; cites
    # the seeded agent so the "references actual trade data" claim is real.
    def fake_llm(system_prompt, user_prompt):
        assert "jade_hawk" in user_prompt  # tool results reached the LLM
        return "jade_hawk leads the desk.\nIts win rate is 100.0%.\nOne closed trade so far."

    app.state.llm_fn = fake_llm

    client = TestClient(app)
    with client.websocket_connect("/api/ws/chat") as ws:
        ws.send_json({"query": "win rate for jade_hawk"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("done"):
                break

    chunk_frames = [f for f in frames if "chunk" in f]
    done_frame = frames[-1]
    assert len(chunk_frames) >= 2, "answer must stream in multiple frames"
    assert done_frame["done"] is True
    assert "jade_hawk" in done_frame["content"]
    assert "".join(f["chunk"] for f in chunk_frames) == done_frame["content"]

    # Both turns persisted to chat_history, in order.
    history = get_chat_history(conn)
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert history[0]["content"] == "win rate for jade_hawk"
    assert "jade_hawk" in history[1]["content"]


def test_chat_falls_back_when_llm_unavailable(conn):
    """The chat must answer from the query tools when the LLM errors or is
    absent — never hard-fail because the model is down."""
    _seed_desk(conn)

    def broken_llm(system_prompt, user_prompt):
        raise ConnectionError("llama-server down")

    answer = compose_chat_answer(conn, "win rate for jade_hawk", broken_llm)
    assert "jade_hawk" in answer  # structured tool answer served instead

    answer_no_llm = compose_chat_answer(conn, "win rate for jade_hawk", None)
    assert "jade_hawk" in answer_no_llm


def test_chat_history_roundtrip(conn):
    save_chat_turn(conn, "user", "desk summary")
    save_chat_turn(conn, "assistant", "The desk is flat.")
    history = get_chat_history(conn)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "The desk is flat."


def test_briefing_includes_regime_memo_when_present(conn):
    """The briefing consumes the risk officer's persisted regime memo
    (M9 crit 7a → crit 8 'regime alerts')."""
    _seed_desk(conn)
    from meta.risk_officer import RiskOfficer

    officer = RiskOfficer(conn, {"desk": {"max_gross_exposure_mult": 2.0}})
    officer.persist_regime_memo({
        "generated_at": "2026-07-14T00:00:00Z",
        "regime_tag": "range_high_vol",
        "average_volatility": 0.02,
        "average_funding": -0.001,
        "crypto_fear_index": 42,
    })

    brief = generate_morning_brief(conn)
    assert "REGIME (risk officer memo)" in brief["briefing_text"]
    assert "range_high_vol" in brief["briefing_text"]
    assert brief["regime_memo"]["regime_tag"] == "range_high_vol"
