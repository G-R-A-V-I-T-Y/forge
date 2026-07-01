# Heartbeat Wiring — Design (Task B)

Follow-up to `2026-07-01-heartbeat-market-data-design.md` (Task A, merged as
`3b496aa`). Task A built `market/heartbeat.py` — the shared generator and its
`data/heartbeat.json` file — but explicitly did not wire anything to read it.
This task rewires the three live consumers (`agents/decision_loop.py`,
`execution/paper_bridge.py`, `web/app.py`'s `/api/prices`) to read the
heartbeat file instead of calling the provider directly, schedules the
generator itself in `forge.py`, and applies the 5-minute wake cadence that
was already sitting unused in `config.yaml`.

## 1. Scheduling the generator (`forge.py`)

A new APScheduler job runs `heartbeat.generate_heartbeat(provider, config)`
on an `IntervalTrigger(seconds=desk.heartbeat_interval_seconds)`, added to
the same `AsyncIOScheduler` instance `main()` already builds for agent
wakes. `IntervalTrigger` has no "run once now" flag, so instead of fighting
it, `generate_heartbeat()` is awaited once directly, synchronously, before
`scheduler.start()` — this both guarantees the first packet exists before
any agent's first wake fires (agents are staggered starting immediately) and
avoids relying on APScheduler's `next_run_time` timing semantics for
something this load-bearing. The scheduled job then repeats every
`heartbeat_interval_seconds` after that. Each cycle logs one INFO line with
the asset count written and the packet's own timestamp, matching the
existing `forge.py` logging style (`"Scheduled %s — wakes every %ds"`, etc).

## 2. Staleness-aware reader (`market/heartbeat.py`)

Added `read_heartbeat_or_none(path, max_age_seconds)`, built on the existing
`read_heartbeat(path)` (unchanged). It parses the packet's `timestamp`
(`"%Y-%m-%dT%H:%M:%SZ"`, UTC, same format `generate_heartbeat` writes) and
compares against `datetime.now(timezone.utc)`. Returns `None` if the file is
missing, unparseable, has no/garbled `timestamp`, or `age_seconds >
max_age_seconds`. Every call site computes `max_age_seconds` as
`2 * desk.heartbeat_interval_seconds` (tolerate one missed cycle) rather than
hardcoding it, reading `heartbeat_interval_seconds` from `config["desk"]`
with the same 300s default `generate_heartbeat` already uses.

## 3. `agents/decision_loop.py`

`run_decision()` no longer calls `provider.get_market_state(assets)`. It
calls `read_heartbeat_or_none(heartbeat_path, max_age_seconds)` up front; if
that returns `None` the function returns
`{"action": "wait", "detail": "heartbeat unavailable or stale"}` immediately
— no fallback to the live API, by design (that would defeat Task B's whole
point).

The old `market_state` shape (`{asset: {ohlcv_15m/1h/4h, mid_price, bid,
ask, funding_rate_current, funding_rate_8h_history, open_interest_usd,
open_interest_24h_change_pct, liquidation_volume_1h_usd,
liquidation_direction_dominant}, "_regime": tag}`) is structurally different
from the heartbeat packet (`{timestamp, assets: {asset: {...}},
cross_asset: {...}, regime: {...}}`). Rather than force the heartbeat packet
into the old shape (which would mean re-deriving `_regime` as a bare string
and dropping most of the richer heartbeat fields before they ever reach the
prompt builder), `build_decision_prompt` and `write_entry`'s call site were
updated to consume the heartbeat shape directly — see sections 4 and
"Regime tag" below.

## Regime tag decision

Task A's heartbeat computes a `regime` *object* (`crypto_fear_index,
btc_dominance, average_volatility, average_funding, average_oi_growth,
market_breadth, risk_on_score, trend_score`) — a set of continuous
composites, not the categorical tag (`trending_bull` / `trending_bear` /
`range_low_vol` / `range_high_vol` / `crisis`) that `market/regime.py`'s
`classify_regime()` produces from 30 days of BTC daily candles.
`store/fingerprint.py::write_entry()` and the trade-bank query section of
`prompt_builder.py` (`query_trades(..., regime=...)`) both depend on that
categorical tag, and `tests/test_query.py` / `tests/test_fingerprint.py`
assert on it as a plain string — so it cannot be silently dropped.

Decision: `generate_heartbeat()` now also fetches BTC's 30-day daily OHLCV
(`get_ohlcv("BTC-PERP", "1d", 30)`, wrapped in the same `_safe()` fallback as
every other per-cycle fetch) and calls `classify_regime()` on it once per
heartbeat cycle, storing the result as `regime["regime_tag"]` alongside the
existing composite fields. This keeps the categorical tag available to
downstream consumers (`decision_loop.py` reads
`heartbeat["regime"]["regime_tag"]` and passes it to `write_entry(...,
regime=...)` exactly as before) while preserving the "heartbeat is the only
thing that calls the provider" invariant — `decision_loop.py` and
`prompt_builder.py` never touch `provider` for market data anymore. The
alternative (having `decision_loop.py` call `classify_regime()` itself via a
live `provider.get_ohlcv` fetch) was rejected because it would reintroduce a
per-agent-wake live API call, the exact thing this task removes.

## Fingerprint snapshot shape

`write_entry()`'s SQL columns (`ohlcv_15m_40_blob`, `funding_history_blob`,
`oi_data_json`, `liquidation_data_json`, ...) were designed around the old
`market_state` per-asset shape. The heartbeat's per-asset fields are richer
in some ways (returns at 4 horizons, technicals, order-book depth, trade-tape
stats) but do not include raw OHLCV candle arrays or liquidation data —
Task A's heartbeat reads the trade tape (`get_recent_trades`) instead of
`get_liquidations` for its buy/sell-volume fields, by design (see Task A's
design doc). `write_entry()` itself is left unchanged (its own tests in
`tests/test_fingerprint.py` still pass unmodified data). `decision_loop.py`'s
call site now builds an adapter dict from the heartbeat asset fields:
`funding_rate_current` and `open_interest_24h_change_pct`-shaped fields map
from `funding` and `oi_zscore`-derived context where a direct equivalent
exists; `ohlcv_15m/1h/4h` and `liquidation_volume_1h_usd` /
`liquidation_direction_dominant` have no heartbeat equivalent and are stored
as empty/zeroed placeholders (`write_entry`'s existing `.get(key, default)`
calls already tolerate missing keys). This is a real information trade-off,
called out explicitly rather than silently absorbed: fingerprints written
going forward will have empty OHLCV blobs and liquidation JSON. A follow-up
could extend `write_entry`'s schema to store the richer heartbeat snapshot
instead (e.g. as a new `market_context_json` blob, which the trades table
already has a column for and which `write_entry` currently leaves alone) —
out of scope for this task, which is about rewiring consumers, not
redesigning the fingerprint schema.

## 4. `agents/prompt_builder.py`

`build_decision_prompt()`'s market-data section now takes the heartbeat
packet (`{"assets": {...}, "cross_asset": {...}, "regime": {...}}`) instead
of `market_state`. For each asset in the agent's universe it renders price,
5m/24h return, funding, RSI, and depth imbalance — not the full ~29-field
list per asset, which would bloat the prompt for no decision-relevant gain —
plus a compact cross-asset block (`market_breadth`, `leader`, `laggard`,
`sector_strength`) and the regime block (`regime_tag` plus
`risk_on_score`/`trend_score`). A new fixed paragraph states the cadence
explicitly, per the captain's hard requirement:

> Market data refreshes every 5 minutes; you cannot see or act on price
> movements faster than this. Do not assume intraday granularity finer than
> 5 minutes when reasoning about entries or exits.

The Portfolio/performance section (account balance, drawdown, open
positions, closed trades, trade-bank queries) is untouched — it still reads
from SQLite via `store/performance.py` / `store/query.py` exactly as before.

## 5. `execution/paper_bridge.py`

`_fill_price(asset)` now reads `heartbeat["assets"][asset]["price"]` via
`read_heartbeat_or_none` instead of calling
`provider.get_orderbook`/`get_mid_price`. If the heartbeat is missing, stale,
or the asset isn't present in it, it raises
`RuntimeError("heartbeat data unavailable or stale; cannot simulate fill")`.
`run_decision()`'s existing broad `except Exception` around the whole
decision body already catches this and returns
`{"action": "error", "detail": str(exc)}` — verified by tracing the call
path (`enter()`/`close()` call `_fill_price()` with no inner try/except of
their own, so the exception propagates up through `bridge.enter()` /
`bridge.close()` in `run_decision()`, which is inside the outer `try`).
No new catch was needed; this was confirmed, not assumed.

## 6. `web/app.py` — `/api/prices`

Reads `assets.{asset}.price` for each universe asset from the heartbeat file
via `read_heartbeat_or_none`, returning `{}` (unchanged failure-mode
contract) if the heartbeat is missing/stale. `/health` gained one small,
additive field, `heartbeat_age_seconds` (`None` if the file is missing or
unparseable), computed independently of the staleness cutoff so it's a raw
freshness indicator rather than a boolean gate — kept intentionally minimal
per the brief's "don't over-scope this task with new UI features" guidance.

## Staleness policy summary

`max_age_seconds = 2 * heartbeat_interval_seconds` (default 600s) everywhere
staleness is checked: decision loop, paper bridge, `/api/prices`. One missed
cycle is tolerated; two is not. This is a single judgment call applied
uniformly rather than three different thresholds per consumer.

## Config / cadence

`config.yaml`'s `desk.wake_interval_seconds: 300` was already set by Task A
and `forge.py`'s `get_agent_wake_interval()` already reads it — confirmed,
no code change needed there. `README.md`'s "every 60 seconds" line (the only
stale cadence claim found outside historical milestone narrative in
`docs/FORGE_PROPOSAL.md`, which documents completed work as originally
specified and is left as a historical record) was updated to "every 5
minutes".
