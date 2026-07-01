# Forge — Evolutionary Prop Trading System

An autonomous AI trader-agent ecosystem. Agents paper-trade crypto perpetuals,
evolve their strategies through thesis reflection, and compete for live capital.

## Milestone 1: Walking Skeleton

### Install
```bash
pip install -r requirements.txt
```

### Run
```bash
python forge.py
```

Open http://localhost:8000 to see jade_hawk making stub trades every 60 seconds.

## Requirements

- Python 3.11+
- (Milestone 2+) Ollama with `qwen3:35b` model
- (Milestone 2+) Hyperliquid API access

## Architecture

```
forge.py → APScheduler → AgentRuntime.tick() → decision_loop → paper_bridge → SQLite
       └→ uvicorn → web/app.py (FastAPI) → SQLite (read-only)
```

## LLM Backend

LLM decisions are dispatched by `llm/client.py` which selects the backend via `config.yaml`:

```yaml
llm_backend: stub        # "stub" (default) or "ollama"
```

- `stub` — hardcoded SOL long decision (deterministic, keeps tests fast)
- `ollama` — async POST to `localhost:11434/api/chat` with `qwen3:35b` model

### Ollama Setup

To use the real LLM backend:

1. Install [Ollama](https://ollama.com) for your platform
2. Pull the model: `ollama pull qwen3:35b`
3. Start Ollama (it runs as a background service by default on port 11434)
4. Set `llm_backend: ollama` in `config.yaml`
5. Run forge: `python forge.py`

The Ollama client (`llm/ollama_client.py`) uses a 30-second timeout, sends system prompt + decision prompt as a chat conversation, and extracts JSON from the model response via regex fallback. On timeout or parse failure it falls back gracefully to a `wait` decision.

Market data is supplied by `market/provider.py` (`MarketProvider`), which selects the backend via `config.yaml`:

```yaml
data_source: stub        # "stub" (default) or "hyperliquid"
```

- `stub` — deterministic in-memory data via `StubMarket` (keeps all existing tests passing)
- `hyperliquid` — live REST API via `HyperliquidClient` with circuit breaker and rate-limit retry

## Performance Metrics

`store/performance.py` computes rolling metrics from closed trades: win rate, profit factor, average win/loss %, Sharpe ratio, best/worst trade, last-20 performance, and last-7-day return. These are injected into the agent's decision prompt so the LLM can self-evaluate its recent track record.

## Trade Fingerprint Store

`store/fingerprint.py` writes a full market snapshot on every trade: `write_entry()`
captures OHLCV candles (15m/1h/4h, msgpack-compressed), funding rate history, OI and
liquidation data, regime tag, and reasoning fields; `write_outcome()` fills in exit
price, PnL, and postmortem when a trade closes. `store/query.py` builds filtered
queries over that history — `query_trades()` (filter by agent, asset, direction,
regime, outcome, date range, funding rate, OI change), `query_win_rate()` (win rate,
total trades, profit factor), `query_all_agents()` (cross-agent, `agent_id=None`),
and `format_trades_summary()` (LLM-readable text block for agent prompts). The same
`query_trades()` powers the `/trades` dashboard page and the `/api/query` endpoint
(GET with query params or POST with a JSON body).

Run `scripts/verify_db_size.py` to confirm the SQLite file stays under the 50MB
budget at 500 full trades.

## Milestones

- **M1** (complete): Walking skeleton — stub LLM, stub market data, paper trading, web UI
- **M2** (complete): Real Hyperliquid market data — `HyperliquidClient`, `MarketProvider`, `StubMarket`
- **M3** (complete): Real LLM decisions (Qwen3.6-35B via Ollama) + performance metrics
- **M4** (complete): Trade fingerprint store — OHLCV/funding/OI snapshots, `store/query.py`, `/trades` page, `/api/query`
- **M5-M10**: Full system
