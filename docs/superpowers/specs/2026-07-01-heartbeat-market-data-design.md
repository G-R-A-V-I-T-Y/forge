# Heartbeat Market-Data Generator — Design (Task A)

Approved by the captain in a brainstorming dialogue prior to this PR. This is
Task A of a two-part change. **Task B — wiring `decision_loop`, the paper
bridge, and the `/api/prices` web ticker to read the heartbeat file instead
of calling the provider directly, and actually changing the agent wake
cadence to match `wake_interval_seconds` — is a separate follow-up PR, not
included here.** This task only builds the generator and the file it writes.

## Problem

Today every agent independently calls `provider.get_market_state()` on its
own wake cycle, each of which hits the Hyperliquid API fresh for all
universe assets. As the desk scales to multiple agents (Milestone 5, already
built) and eventually many more (future milestones), this multiplies API
calls per agent per cycle. The fix: one shared "heartbeat" snapshot,
computed once every `heartbeat_interval_seconds` (default 300s / 5 minutes)
by a single system-wide job, written atomically to `data/heartbeat.json`.
All consumers will read this file. That consumer-side rewiring is Task B.

## Scope of this task

1. `requirements.txt`: add `numpy` and `pandas` pins.
2. `config.yaml`: replace `universe` with the new 20-asset list, add
   `heartbeat_interval_seconds` / `heartbeat_path` under `desk:`, bump
   `wake_interval_seconds` to 300 (config value only — Task B applies it).
3. `market/hyperliquid.py`: add `get_funding_history()` thin wrapper around
   the already-documented `fundingHistory` endpoint.
4. `market/stub.py`: add deterministic `get_funding_history()` (and a plain
   `get_recent_trades()`, see below) so the stub backend satisfies every
   method `heartbeat.py` needs, keeping `tests/test_heartbeat.py` network-free.
5. `market/heartbeat.py`: the generator itself.
6. `tests/test_heartbeat.py`.

## `market/heartbeat.py` structure

Split into small, single-purpose pieces rather than one large function,
matching the "smaller well-bounded units" style of `market/regime.py` and
`store/query.py`:

- `indicators.py`-style pure functions embedded at module scope (not a
  separate file — the whole module is ~one cohesive unit at this size):
  `_ema`, `_rsi`, `_atr`, `_realized_vol`, `_zscore`, `_vwap_distance`,
  `_log_returns` — each takes plain lists/arrays and returns a float or
  `None`, independently testable.
- `_fetch_asset_snapshot(provider, asset, now_ms)` — does all I/O for one
  asset (OHLCV 5m×300, funding history, orderbook, recent trades) and
  returns the raw materials; wrapped in the existing `_safe()` fallback
  pattern from `market/provider.py` so a single asset's failure fills that
  asset's fields with `None` instead of crashing the cycle or dropping the
  asset from the packet.
- `_compute_asset_fields(raw, oi_history_for_asset)` — pure computation from
  the raw snapshot into the exact per-asset field dict.
- `_compute_cross_asset(assets_fields)` — breadth, correlation matrix, PCA,
  sector strength, momentum rankings, relative strength.
- `_compute_regime(assets_fields, cross_asset, oi_history, fear_index)` —
  regime-level composites.
- `_fetch_fear_greed()` — isolated httpx call to `alternative.me`, 5s
  timeout, try/except returning `None` on any failure (third-party
  dependency outside Hyperliquid; must never block/crash the cycle).
- `_load_oi_history(path)` / `_save_oi_history(path, history)` — the rolling
  OI-sample state file.
- `generate_heartbeat(provider, config) -> dict` — the async orchestrator:
  fetch all 20 assets concurrently, update OI history, compute cross-asset +
  regime, assemble the packet, atomic-write it, return it.
- `write_heartbeat(path, packet)` / `read_heartbeat(path) -> dict | None` —
  the atomic write/read contract.

## Exact field list

Per-asset (all 20 universe assets, each under `assets["<ASSET>-PERP"]`):

```
price, return_5m, return_30m, return_4h, return_24h, volume,
open_interest, funding, spread, atr, realized_vol, rsi,
ema20, ema50, ema200, vwap_distance, volume_zscore, funding_zscore,
oi_zscore, bid_depth, ask_depth, depth_imbalance, top5_imbalance,
slippage_estimate, buy_volume, sell_volume, aggressor_ratio,
avg_trade_size, largest_trade
```

Cross-asset (`cross_asset` object):

```
market_breadth, average_return, median_return, leader, laggard,
correlation_matrix, pca, sector_strength, momentum_rankings,
relative_strength
```

Regime (`regime` object):

```
crypto_fear_index, btc_dominance, average_volatility, average_funding,
average_oi_growth, market_breadth, risk_on_score, trend_score
```

These are the exact JSON key names Task B's consumers will depend on — not
renamed or restructured here.

### Data sourcing

- **Lookback window: 300 5m candles (25h)**, fetched once per asset per
  cycle via `get_ohlcv(asset, "5m", 300)`. This single fetch covers
  EMA200, ATR(14), RSI(14), realized vol, and all Z-score baselines.
- **Funding history**: `get_funding_history(asset, start_time_ms=now-25h)`
  for the funding Z-score baseline.
- **Recent trades**: a new `get_recent_trades(asset, hours)` method is added
  to `HyperliquidClient` and `StubMarket` returning the raw trade list
  (`{side, price, size, ts}`) without the liquidation-proxy renaming that
  `get_liquidations` applies. `get_liquidations` was considered and reused
  as-is instead of duplicating the HTTP call, since its return shape
  (`side/price/size/ts`) already has everything the buy/sell-volume and
  aggressor fields need — but `get_liquidations` hardcodes an `hours`
  default of 4 and a `_proxy` marker field aimed at the liquidation-proxy use
  case, so a plain `get_recent_trades` is added instead to keep the
  heartbeat's ~1h trade-tape window semantically separate from the
  liquidation-proxy naming (documented in code). Trade tape window: last 1
  hour — a reasonable window for trade-tape aggregates given the 5-minute
  heartbeat cadence.
- **Order book**: `get_orderbook(asset, depth=5)` for bid/ask depth,
  imbalance, and slippage estimate. `top5_imbalance` is computed from the
  same top-5 levels as `bid_depth`/`ask_depth` (not a separate fetch), so by
  construction it is identical to `depth_imbalance` — both fields are kept
  per spec for Task B's consumers.
- **Slippage estimate**: walks the order-book levels for a fixed $10,000
  reference notional, computing the size-weighted average fill price vs.
  mid, returned as a pct difference.
- **Side field convention**: Hyperliquid's real `recentTrades` endpoint
  returns `side` as `"B"`/`"A"` (buy/ask-side aggressor); the existing stub
  in `market/stub.py` used `"long"`/`"short"` for the liquidation-proxy use
  case. `market/heartbeat.py` treats `"B"` or `"buy"` or `"long"` as a buy
  and everything else as a sell, so both real and stub data classify
  correctly without assuming one fixed vocabulary.

## Documented approximations (called out explicitly)

- **`oi_zscore` / `average_oi_growth`**: Hyperliquid has no OI *history*
  endpoint. This is approximated by sampling current OI once per heartbeat
  cycle and maintaining a rolling per-asset window (capped at 100 samples)
  in `data/heartbeat_oi_history.json` (`{asset: [oi_values...]}`), read +
  append + trim + write each cycle. Z-scores and growth rates against this
  window are therefore only as good as the sampling history accumulated
  since this feature was deployed (thin/absent on first runs) — noted
  in-code and here as a deliberate workaround, not a substitute for a real
  OI history API.
- **`btc_dominance`**: Hyperliquid has no market-wide dominance endpoint.
  Approximated as `BTC-PERP`'s `open_interest` divided by the sum of
  `open_interest` across the 20 tracked assets — i.e. dominance *within the
  tracked universe*, not true market-wide BTC dominance across all crypto.
- **`risk_on_score`** (heuristic, not authoritative quant theory):
  `market_breadth * 0.5 + (1 if average_funding > 0 else 0) * 0.25 +
  (1 - min(average_volatility / 1.0, 1)) * 0.25`. The `1.0` (100% annualized
  vol) is a fixed reference constant used only to normalize into 0-1; retune
  later as needed.
- **`trend_score`** (heuristic, same caveat):
  `average_return / average_volatility if average_volatility > 0 else 0.0`
  — a simple risk-adjusted trend measure, not validated against any
  backtest.
- **PCA**: implemented via numpy eigendecomposition of the covariance matrix
  of 5m returns (no `scikit-learn` dependency added). Top 3 components.

## Sector grouping (exact, per spec)

```
L1:              BTC, ETH, SOL, SUI, AVAX, ADA, BNB
L2:              ARB, OP
Modular_DA:       TIA
DeFi_Oracle:      AAVE, LINK
AI:               FET, RENDER, TAO
Exchange:         HYPE
Legacy_Payments:  XRP, XLM, LTC, BCH
```

All 20 universe assets are covered exactly once across these 7 sectors.

## Atomic write contract

`write_heartbeat(path, packet)`: writes JSON to `f"{path}.tmp"`, then
`os.replace(tmp_path, path)`. `os.replace` is atomic on both POSIX and
Windows (this repo's `AGENTS.md` documents the project targets Windows
via Anaconda Python), so no reader ever observes a half-written file. Parent
directories are created as needed (`data/` already exists in this repo).

`read_heartbeat(path) -> dict | None` reads and JSON-parses the file,
returning `None` (after logging a warning) if the file is missing or fails
to parse. Task B's consumers will compare the packet's `timestamp` against
wall-clock time to detect staleness — that check is Task B's concern; this
task only guarantees `read_heartbeat` returns a clean `dict` or `None`.

## Entry point

```python
async def generate_heartbeat(provider: MarketProvider, config: dict) -> dict:
    ...
```

Does all fetching, computing, and the atomic write; returns the packet
dict. Useful for tests now and for wiring into APScheduler in Task B. This
task deliberately does **not** wire it into `forge.py`'s scheduler.

## Testing approach

- Each indicator function tested in isolation against hand-computed/known
  values (EMA, RSI, ATR, Z-score, realized vol) via `pytest.approx`.
- Correlation matrix / PCA tested against a small synthetic multi-asset
  return matrix with a known correlation structure (perfectly correlated
  series → correlation ≈ 1.0).
- `sector_strength` tested for exactly 7 keys, no assets missing/duplicated.
- `generate_heartbeat()` end-to-end against `market/stub.py` (`data_source:
  stub`), confirming the full `timestamp` / `assets` / `cross_asset` /
  `regime` structure and all 20 universe assets present with every listed
  field.
- Atomic write / `read_heartbeat` round-trip, and `read_heartbeat` on a
  missing path returning `None` without raising.
- Fear & Greed fetch failure handled gracefully (mocked/monkeypatched to
  raise) — `crypto_fear_index` becomes `None`, cycle still completes.
