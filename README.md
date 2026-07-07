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

Open http://localhost:8000 to see jade_hawk making stub trades every 5 minutes (the shared heartbeat market-data cadence).
On a fresh database `forge.py` seeds just `jade_hawk`; run
`python scripts/fresh_start.py` first to reset and launch the full 10-agent
desk instead (see [Multi-Agent Desk](#multi-agent-desk)).

## Requirements

- Python 3.11+
- (Milestone 2+) A local `llama-server` binary + GGUF model (see [Local LLM Server & Settings](#local-llm-server--settings)), or Ollama with `qwen3:35b` for the legacy single-backend path
- (Milestone 2+) Hyperliquid API access

## Architecture

```
forge.py → APScheduler → AgentRuntime.tick() → decision_loop → paper_bridge → SQLite
       └→ uvicorn → web/app.py (FastAPI) → SQLite (read-only)
```

## LLM Backend

Production agent decisions go through `llm/model_chain.py`'s `decide()`, which tries an ordered
fallback chain of models (several `opencode` CLI tiers, then a local last-resort tier) and reports
which model actually answered. The chain is loaded from the `settings` DB on every call — see
[Local LLM Server & Settings](#local-llm-server--settings) below — and falls back to a hardcoded
default chain if the DB is unavailable. See
`docs/superpowers/specs/2026-07-01-model-fallback-chain-design.md` for the full design.

**M6 model pinning:** `decide()` also accepts an `agent_id`. If that agent has a `pinned_model`
(checked in its `agents.config_json`, then in the `settings` table under `agent_{agent_id}_model`),
only that model is tried — no fallback chain — so the agent's decisions stay reproducible across
runs. Agents without a pinned model keep using the ordered fallback chain as before.

Independently, `llm/client.py` offers a simpler single-backend path selected via `config.yaml`,
used mainly by older tests and scripts:

```yaml
llm_backend: stub        # "stub" (default) or "ollama"
```

- `stub` — hardcoded SOL long decision (deterministic, keeps tests fast)
- `ollama` — async POST to `localhost:11434/api/chat` with `qwen3:35b` model

The Ollama client (`llm/ollama_client.py`) uses a 30-second timeout, sends system prompt + decision prompt as a chat conversation, and extracts JSON from the model response via regex fallback. On timeout or parse failure it falls back gracefully to a `wait` decision.

## Local LLM Server & Settings

The last-resort tier in the model chain is a forge-managed `llama-server` subprocess
(`llm/llama_server.py`, module-level singleton `server_manager`), which replaced the old Ollama
tier because running with `--reasoning off` cuts per-decision latency from ~160-290s to ~12-20s
with no measured quality loss. `llm/llama_server_client.py` talks to it over its OpenAI-compatible
`http://localhost:{port}/v1/chat/completions` endpoint.

Configure it from the **Settings** page (`/settings`) in the web UI:

- Toggle **spawn on startup** so forge launches the server automatically, or start/stop it live.
- Edit context size, batch/ubatch size, thread count, `--reasoning` on/off, and GPU layers. Tuned
  defaults (`batch_size=2048`, `ubatch_size=1024`, `context_size=24576`) come from empirical
  testing; `context_size` cannot go below 12288 (real prompts run ~10-11k tokens).
- Set the `llama-server` binary path and the GGUF model path — both required before the server
  can start, with a clear error if unset or missing.
- Drag to reorder the model fallback chain used by `model_chain.decide()`.

**Save & Apply** persists settings to the `settings` SQLite table (`store/settings.py`) and
restarts the local server immediately if it's running, so changes take effect without a forge
restart. Model chain edits take effect on the very next agent decision cycle.

Market data is supplied by `market/provider.py` (`MarketProvider`), which selects the backend via `config.yaml`:

```yaml
data_source: stub        # "stub" (default) or "hyperliquid"
```

- `stub` — deterministic in-memory data via `StubMarket` (keeps all existing tests passing)
- `hyperliquid` — live REST API via `HyperliquidClient` with circuit breaker and rate-limit retry

`execution/paper_bridge.py`'s fill price applies half the heartbeat `spread` against the
position's direction (long entries pay up, short entries pay down) plus a `slippage_estimate`
in the same direction, for a more realistic paper fill than the raw heartbeat price.
`store/positions.py` also records `duration_minutes` on every closed trade.

## Risk Gate

`risk/gate.py`'s `validate_order()` enforces 13 hard rules before an order reaches paper
execution, raising `RiskViolation` on the first breach: SL/TP presence, SL/TP geometry bracketing
the entry price, minimum SL distance (0.3%), minimum TP distance to clear the round-trip fee
hurdle (0.5%), minimum reward:risk ratio (0.5), leverage cap, position-size cap, a notional
exposure cap (`position_size_pct × leverage <= 2.0`), concurrent-positions cap, entry-price
deviation from the live heartbeat price (max 0.5%, skipped if no heartbeat price is available),
liquidation distance (>= 2x SL distance), and a confidence floor (>= 0.50 when present). Corrupted
trades that predate these rules are voided rather than deleted: `store/db.py`'s
`void_corrupted_trades()` sets `trades.voided = 1` and a `void_reason` (missing SL/TP, bad
geometry, too-tight SL, or sub-fee-hurdle TP); `store/performance.py` excludes voided trades from
all metrics.

## Performance Metrics

`store/performance.py` computes rolling metrics from closed, non-voided trades: win rate, profit
factor (capped at 10 to keep leaderboards from being skewed by an infinite ratio), average
win/loss %, an equity-curve Sharpe ratio, Sortino ratio (downside deviation), exposure-adjusted
return (total PnL over mean notional exposure), a benchmark-vs-null figure (total PnL vs. a
zero-return null strategy), best/worst trade, last-20 performance, and last-7-day return. These
are injected into the agent's decision prompt so the LLM can self-evaluate its recent track
record.

`scripts/seed_benchmarks.py` seeds two benchmark agents for leaderboard comparison:
`benchmark_random_walk` (random long/short decisions) and `benchmark_btc_hold` (BTC-only,
holds indefinitely). Both compete on the same leaderboard as the AI agents.

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

## Historical Heartbeat Capture

Every heartbeat packet written to `data/heartbeat.json` is also appended as one
JSON line to a daily `data/historical_data/{YYYY-MM-DD}.jsonl` file (UTC date
from the packet's timestamp), building a continuous market-data history for
later research and backtesting. This capture path is failure-isolated: any
error is logged and swallowed so it can never block or degrade the primary
heartbeat write. The directory is gitignored.

Run `scripts/build_training_dataset.py` to turn that JSONL history into a
flat training dataset: `python scripts/build_training_dataset.py [--start-date
YYYY-MM-DD] [--end-date YYYY-MM-DD] [--output PATH] [--horizons MIN [MIN ...]]
[--sl-pct PCT] [--tp-pct PCT]`. It flattens each (asset, timestamp) sample and
computes forward-looking labels at each horizon (default 30m/2h/4h/24h):
return, realized volatility, max drawdown/run-up, funding accrued, and an
illustrative stop-loss/take-profit "which triggers first" label (default
2%/5%). A (sample, horizon) combination is excluded (labels left null, row
kept) when the timeline has a gap or doesn't yet extend far enough past
horizon. Output is written as Parquet (default
`data/historical_data/training_dataset.parquet`; requires `pyarrow`). This is
an offline, read-only batch job — it is not wired into `forge.py` or run on a
schedule.

## Multi-Agent Desk

`forge.py` reads every `ACTIVE`/`ROOKIE` agent row from SQLite at startup and
launches one `AgentRuntime` per agent, each on its own APScheduler job. Wakes
are staggered 30 seconds apart (agent index × 30s) to avoid simultaneous
Hyperliquid API bursts. Each agent's wake interval is read from its
`config_json` column on every tick (falling back to the desk default in
`config.yaml`), so changing an agent's interval takes effect on its next wake
without a restart; `AgentRuntime` reschedules its own APScheduler job when the
interval changes.

`store/positions.py` exposes `get_all_open_positions()` and
`get_desk_positions_summary()`, giving every agent visibility into what every
other agent currently holds. Competing positions — multiple agents in the same
asset, including opposing directions — are allowed by design: divergent
theses in the same asset are treated as signal and provide natural desk-wide
hedging, so `risk/gate.py` does not block them.

`meta/spawner.py` creates new agents: `spawn_agent()` inserts the agent row,
writes the seed thesis file, and creates the starting paper account;
`generate_agent_name()` picks an unused adjective_animal name;
`check_against_graveyard()` is a stub that always reports the thesis as
unique (a real similarity check lands in a later milestone).

To reset the desk and seed all 10 initial agents (`iron_moth`, `silver_basin`,
`copper_vane`, `gray_finch`, `amber_wolf`, `steel_crane`, `onyx_heron`,
`jade_hawk`, `violet_lion`, `crimson_fox`), each with a distinct specialized
strategy (cross-sectional momentum, funding dislocation, OI intelligence,
order book microstructure, trade flow, liquidation hunting, relative value,
regime detection, volatility trading, and a meta agent that weights the other
nine), with their seed theses:

```bash
python scripts/fresh_start.py
```

This deletes the existing `data/forge.db` (and any WAL/SHM sidecar files),
re-initializes the schema, and seeds each agent via `spawn_agent()`. Pass
`--yes` to skip the confirmation prompt. `python forge.py` will then launch
all 10 agents concurrently.

The `/` overview page shows a sortable leaderboard, rendered as a LyteNyte
Grid with a per-agent balance sparkline column, across all agents, and
`/agents/{name}` shows a full detail page per agent. The leaderboard is
backed by `GET /api/desk` for row data and `GET /api/agents/balance-history`
for sparkline history (or `GET /api/agents/{name}/balance-history` for a
single agent), and `web/static/forge.js` opens a `WS /api/ws/desk` connection
on page load that pushes the same desk summary every 30 seconds so the grid
updates live without a page reload.

A nightly APScheduler job (`forge.py`, cron 02:00 UTC) runs a counterfactual analysis for each
agent's most recent `wait` decision: `agents/decision_loop.py`'s `run_counterfactual()` asks the
LLM whether taking the trade would have been profitable and stores the result on the `decisions`
row (`counterfactual_result`, `counterfactual_was_better`). Every decision is logged via
`log_decision()` into the `decisions` table (`agent_id`, `timestamp`, `decision_action`,
`decision_reason`, `decision_details_json`).

`config.yaml`'s `desk.starting_balance` is the single source of truth for new-agent starting
balance; `scripts/fresh_start.py` and `scripts/seed_benchmarks.py` both read it from there instead
of hardcoding their own value.

## Milestones

- **M1** (complete): Walking skeleton — stub LLM, stub market data, paper trading, web UI
- **M2** (complete): Real Hyperliquid market data — `HyperliquidClient`, `MarketProvider`, `StubMarket`
- **M3** (complete): Real LLM decisions (Qwen3.6-35B, now via the forge-managed llama-server) + performance metrics
- **M4** (complete): Trade fingerprint store — OHLCV/funding/OI snapshots, `store/query.py`, `/trades` page, `/api/query`
- **M5** (complete): Multi-agent desk — 10 concurrent agents, desk-wide position registry, competing positions allowed, leaderboard (LyteNyte Grid + sparklines) + agent detail pages, `/api/desk`, `/api/agents/balance-history`, `WS /api/ws/desk`
- **M6** (complete): Truth — 13-rule risk gate, voided/corrupted-trade tracking, realistic paper fills (spread/slippage/duration), per-agent model pinning, `decisions` table + nightly counterfactual job, rewritten metrics (Sharpe/Sortino/exposure-adjusted/benchmark-vs-null), seeded benchmark agents, config hygiene
- **M7-M10**: Full system
- **M11** (Phases 1-2 complete): Historical heartbeat data — daily JSONL capture in `data/historical_data/`; offline `scripts/build_training_dataset.py` builds a multi-horizon labeled Parquet dataset; statistical forecast features (Phase 3) planned (see `docs/FORGE_PROPOSAL.md`)
