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

Market data is supplied by `market/provider.py` (`MarketProvider`), which selects the backend via `config.yaml`:

```yaml
data_source: stub        # "stub" (default) or "hyperliquid"
```

- `stub` — deterministic in-memory data via `StubMarket` (keeps all existing tests passing)
- `hyperliquid` — live REST API via `HyperliquidClient` with circuit breaker and rate-limit retry

## Milestones

- **M1** (complete): Walking skeleton — stub LLM, stub market data, paper trading, web UI
- **M2** (complete): Real Hyperliquid market data — `HyperliquidClient`, `MarketProvider`, `StubMarket`
- **M3**: Real LLM decisions (Qwen3.6-35B via Ollama)
- **M4-M10**: Full system
