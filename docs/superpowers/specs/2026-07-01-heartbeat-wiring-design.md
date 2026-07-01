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

## Addendum: consolidated trade-thumbprint capture (captain review, PR #11)

The captain's review of this PR drew a distinction the "Fingerprint
snapshot shape" section above under-delivered on: heartbeat packets are a
*shared, overwritten* market snapshot, but the moment an agent trades on
that snapshot, the portfolio-level state + the heartbeat's non-asset and
asset-specific fields *at that instant* become part of the trade's
permanent record — queryable later by other agents and by the web UI. This
addendum documents the follow-up work implementing that.

### A. OHLCV candles now ride along in the heartbeat packet

`market/heartbeat.py`'s `_compute_asset_fields()` already fetched 300 x 5m
candles (`raw["candles"]`, ~25h lookback) per asset per cycle to compute
EMA/ATR/RSI/etc., but the raw series itself never reached the written
packet. Each asset's packet entry now also carries:

- `candles_5m`: the full fetched 300-candle series, as-is (no new fetch).
- `candles_30m`: a resample of that same series, 6 consecutive 5m candles
  aggregated per output candle (open = first candle's open, high = max
  high, low = min low, close = last candle's close, volume = sum).
- `candles_4h`: the same resampling with a 48-candle factor.

Both resamples are produced by a new pure, independently-tested helper,
`_resample_candles(candles_5m, factor)` (drops a trailing partial group
rather than emitting an incomplete candle). All three fields were added to
`PER_ASSET_FIELDS` so the existing end-to-end structure test
(`test_generate_heartbeat_end_to_end_structure`) verifies their presence
for every universe asset automatically; new tests in `tests/test_heartbeat.py`
hand-verify the resampling arithmetic and the packet shape.

This does increase `data/heartbeat.json`'s size (300 + 50 + 6 ≈ 356
candles/asset x 20 assets) but the file is overwritten every cycle, not
accumulated, so per the captain's explicit call this needed no compression
or size mitigation — only the persisted trade fingerprints (below) needed
that discipline.

### B. Trade fingerprint: the full context at entry, not just funding/OI

The old `_asset_fingerprint_snapshot()` adapter (which fed `write_entry()`
only `funding_rate_current` and `open_interest_24h_change_pct`) is deleted.
`agents/decision_loop.py` now has `build_trade_market_context(heartbeat,
asset, conn, agent_id, config)`, which assembles one consolidated dict:

```python
{
    "portfolio": {...},    # this agent's cash/equity/exposure/open positions/PnL/risk utilization
    "cross_asset": {...},  # heartbeat's cross_asset block, as-is
    "regime": {...},       # heartbeat's regime block, as-is
    "asset": {...},        # the FULL per-asset heartbeat field dict for the traded asset (~29 fields + candles_5m/30m/4h)
}
```

`portfolio` comes from a new `agents/prompt_builder.build_portfolio_snapshot(conn,
agent_id, config) -> dict` — factored out of the same account/positions/
performance logic the decision prompt's Portfolio section already used, so
both call sites share one implementation (independently unit-tested in
`tests/test_prompt_builder.py`) rather than recomputing portfolio state two
different ways. `cross_asset`/`regime` are passed through unchanged from
the heartbeat already read in `run_decision()`. `asset` is the full
per-asset dict — not the narrow 2-field adapter — pulled straight from
`heartbeat["assets"][traded_asset]`.

`store/fingerprint.py::write_entry()` gained a new `market_context: dict |
None = None` parameter. When provided, the whole dict is msgpack-encoded
(the same compression convention already used for `ohlcv_15m`/`1h`/`4h` and
`funding_history_blob` in this same function) and stored in the existing
`market_context_json` column — chosen over storing it as raw JSON text
specifically because it now carries OHLCV candle arrays. `None` (the
default) leaves the column `NULL`, so existing callers/tests are
unaffected. The legacy `ohlcv_15m`/`ohlcv_1h`/`ohlcv_4h`/
`funding_history_blob`/`oi_data_json`/`liquidation_data_json` columns and
the narrow `funding_rate_current`/`open_interest_24h_change_pct` columns
are still populated exactly as before (via `asset_snapshot`, computed
inline in `run_decision()` from `market_context["asset"]`) — nothing was
removed, only added.

`store/query.py`'s `_decode_row()` now decodes `market_context_json` with
msgpack (moved out of `_JSON_COLUMNS`, which assumes plain JSON text) under
the same `decode_ohlcv` gate as the other OHLCV blobs, so `query_trades()`
and `get_trade()` return it as a plain nested dict for any trade that has
one, and `None` for older rows or lightweight (`decode_ohlcv=False`) list
views. This is what makes the richer capture actually reachable by other
agents' cross-agent queries and by the web UI, not just sitting unused in
the column.

### C. Web UI: candlestick chart backward compatibility

`web/templates/trade_bank.html`'s fingerprint-row chart now prefers
`market_context_json.asset.candles_5m` (via a small `chartCandles(t)`
helper) for any trade that has it, falling back to the legacy `t.ohlcv_15m`
column for trades recorded before this change — so existing rows in
someone's local `data/forge.db` keep rendering instead of showing an empty
chart. The chart label reflects which source was used (`"5m"` vs. `"15m
(legacy)"`). No API changes were needed on the FastAPI side:
`/api/trades/{id}` already calls `get_trade(conn, trade_id,
decode_ohlcv=True)`, which now includes the decoded `market_context_json`
automatically per section B. A single default timeframe (5m) is used; a
30m/4h toggle was considered but skipped as unnecessary frontend scope for
this task — the raw candle data for both is already present in the API
response if a future task wants to add one.

### D. DB size re-verification (Milestone 4 budget)

`tests/test_db_size.py` was updated to build a **realistic**,
heartbeat-shaped `market_context` fixture (not an empty dict) — a full
`asset` dict with real field counts and real candle series (300 x 5m, 50 x
30m, 6 x 4h), a `portfolio` dict with an open position and performance
metrics, and a `cross_asset` block with a 20x20 correlation matrix,
momentum rankings, and relative-strength maps — and passes it via the new
`market_context=` param on every one of the 50 simulated trades.

**Measured result:** 50 trades with this realistic `market_context_json`
payload (plus the pre-existing legacy blob columns) produced a **1.664MB**
database (**~34.1KB/trade**), extrapolating to **~16.6MB at 500 trades** —
comfortably under the existing 50MB budget (vs. the previous ~2.6MB/500
measurement without `market_context_json`). The budget assertion
(`BUDGET_MB_PER_500 = 50.0`) did not need to change; msgpack compression on
the candle-heavy blob keeps the richer capture well within the original
budget. If per-trade size becomes a concern at much larger scale (e.g.
thousands of trades/agent), a follow-up could truncate `candles_4h`/
`candles_30m` before storage (they're the least information-dense of the
three series) — not needed today.
