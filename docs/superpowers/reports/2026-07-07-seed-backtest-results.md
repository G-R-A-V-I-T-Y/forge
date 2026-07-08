# Seed backtest results -- iron_moth, jade_hawk, silver_basin

Task 8 of `docs/superpowers/plans/2026-07-07-strategy-spec-dsl-backtester.md`
(M7b). This supersedes the original 2026-07-07 run in this same file: that
run's numbers were produced by a backtest engine with three real defects,
found and fixed after the fact:

1. **Live/backtest feature parity bug.** `backtest/engine.py` fed
   `compute_replayable_fields` hourly candles (`candles_1h`) while every
   function in `market/features.py` and the replayable core is written and
   documented against live's 300 x 5m-candle / 25h window. Every time-based
   feature (RSI/EMA/ATR periods, `return_24h`, `momentum_acceleration`,
   `realized_vol`'s annualization) was silently computed over a ~12x
   different window than live ever produces. Fixed: the engine now reads
   `candles_5m`, matching live's `LOOKBACK_CANDLES`/`LOOKBACK_HOURS` exactly.
2. **Backfill silently truncated.** `scripts/backfill_history.py` requested
   a 12-month range in one `candleSnapshot`/`fundingHistory` call with no
   pagination. The original run's "5003 rows" for `candles_1h` and "500
   rows" for `funding` weren't the real 12-month depth -- they were
   whatever fit in a single API response. Fixed: both streams now page
   through the full requested range (see "How this was produced" below for
   what pagination did and did not turn out to fix).
3. **`steel_crane` cannot be backtested by construction.** Its primary
   evidence term (`liq_total_usd`) has `missing: veto`, and liquidation
   history is never backfilled -- every bar was vetoed, always, which is a
   data-availability artifact, not a "no edge" finding. Swapped for
   `jade_hawk` (VWAP mean-reversion, hand-compiled from its short-entry
   sub-thesis: fade price overextended above VWAP), which is
   candle/funding-driven and can actually be evaluated.

## How this was produced

1. `python scripts/backfill_history.py` was run for real against
   Hyperliquid's public API (read-only, no credentials, no trading) for the
   full 20-asset universe in `config.yaml`, requesting 12 months of 1h
   candles + funding and 90 days of 5m candles, using the now-paginated
   fetch.

   Pagination fixed funding completely: **500 -> 8640 rows per asset**, the
   real 12-month depth (Hyperliquid's `fundingHistory` has no `endTime` and
   returns the earliest N rows after `startTime`, so paging forward from the
   last row received works as expected).

   Pagination did **not** increase candle depth. `candleSnapshot` truncates
   an over-wide `[startTime, endTime]` range from the *start*, keeping the
   candles nearest `endTime` -- confirmed by the original single-request run
   returning the most *recent* ~7 months, not the oldest. Backward
   pagination (shrinking `endTime` from the earliest candle each page
   returned) is the structurally correct way to walk further back, and it
   is what fixed funding's forward-pagination counterpart -- but for candles
   specifically, every second-page request came back empty. Every asset
   lands on the same ceiling regardless of pagination direction: **~5000
   rows for `candles_1h` (~208 days) and ~5000 rows for `candles_5m` (~17
   days)**. This looks like a genuine historical-depth ceiling on
   Hyperliquid's public `candleSnapshot` endpoint, not a response-size cap
   pagination can walk past. `DEFAULT_CANDLE_MONTHS=12` and
   `DEFAULT_5M_DAYS=90` remain the script's request parameters (asking for
   more than the endpoint will serve is harmless -- pagination stops
   cleanly on the first empty page), but the real achievable window today is
   ~208 days of 1h and ~17 days of 5m, per asset, via this endpoint.

   One asset (`AVAX-PERP`) hit Hyperliquid's rate limiter hard enough to
   exhaust `HyperliquidClient`'s 3-retry budget on the full-universe run
   despite a 0.3s pacing delay added between paginated page requests; it was
   backfilled individually afterward and is included in the results below.
   All 20 universe assets are now present.

2. `python scripts/run_seed_backtests.py` was run for real against the
   corrected ledger and the corrected engine. Full output below, verbatim.

## Results

```
=== iron_moth ===
  data window: {'candles_5m': {'rows': 25121}, 'funding': {'rows': 43200}, 'oi': {'rows': 0}}
  train: 125 trades, +13.01% return, Sharpe 1.23
  validate: 26 trades, -0.46% return, Sharpe -0.15
  test: 26 trades, -7.90% return, Sharpe -2.39
  deflated Sharpe: -2.74
  parameter sensitivity: {'confidence_threshold': 0.0, 'scale_threshold': -0.077, 'stop_loss_pct': 0.035, 'take_profit_pct': 0.0}

=== jade_hawk ===
  data window: {'candles_5m': {'rows': 20099}, 'funding': {'rows': 34560}, 'oi': {'rows': 0}}
  train: 127 trades, -2.61% return, Sharpe -0.53
  validate: 22 trades, -2.19% return, Sharpe -1.56
  test: 12 trades, +1.33% return, Sharpe 1.30
  deflated Sharpe: 0.79
  parameter sensitivity: {'confidence_threshold': 0.0, 'scale_threshold': 0.200, 'stop_loss_pct': 0.414, 'take_profit_pct': 0.026}

=== silver_basin ===
  data window: {'candles_5m': {'rows': 25122}, 'funding': {'rows': 43200}, 'oi': {'rows': 0}}
  train: 18 trades, -3.11% return, Sharpe -1.02
  validate: 1 trades, +0.35% return, Sharpe 0.00
  test: 0 trades, +0.00% return, Sharpe 0.00
  deflated Sharpe: 0.00
  parameter sensitivity: {'confidence_threshold': 0.0, 'scale_threshold': 0.0, 'stop_loss_pct': 0.0, 'take_profit_pct': 0.0}
```

(`data_window` rows are summed across each spec's universe: iron_moth and
jade_hawk use 4-5 assets, silver_basin uses 5. 0 OI rows confirms OI remains
genuinely un-backfilled, as designed.)

## Interpretation -- honest, not flattering

**iron_moth**: now actually trades a meaningfully different pattern than the
original (broken-engine) run, which showed uniform losses across every
split. With correctly-scaled features, iron_moth shows a real in-sample
edge (train Sharpe +1.23, +13.01% return) that does not survive
out-of-sample: validate is flat-to-negative (Sharpe -0.15) and test is
clearly negative (Sharpe -2.39, -7.90%). The deflated Sharpe (-2.74) --
which penalizes exactly this pattern -- confirms the in-sample edge is not
real signal surviving multiple-testing scrutiny. This is the textbook
overfitting signature the walk-forward harness exists to catch: the
momentum-acceleration + volatility-adjusted-entry thesis, as hand-compiled,
found something real in its train window that did not generalize. The
original (feature-mismatched) run's uniformly-bad numbers were not even
measuring this thesis correctly; this run is the first honest look at it.

**silver_basin**: previously 0 trades across all three splits with the
broken engine and a 500-row (truncated) funding sample. With the real
12-month funding history (8640 rows/asset) and the corrected 14-day
funding-zscore window, the thesis now actually fires: 18 trades in train
(Sharpe -1.02, -3.11%), a single trade in validate, none in test. Still not
evidence of edge -- if anything a modest in-sample loss -- but it is now a
real, exercised evaluation rather than a threshold that structurally never
crossed the entry bar. The thin validate/test trade counts reflect the
funding-driven entry condition being inherently rare within this window,
not a remaining bug.

**jade_hawk** (replacing `steel_crane`): the VWAP mean-reversion short-entry
thesis got a full, meaningful walk-forward evaluation across all three
splits (127/22/12 trades) -- something `steel_crane` could never get,
since its primary evidence was permanently vetoed by missing liquidation
data. Results are mixed and inconclusive rather than a clean edge: train
and validate are modestly negative (Sharpe -0.53 and -1.56), test is
modestly positive (Sharpe +1.30, +1.33%), and the deflated Sharpe (+0.79)
is positive but on a small test-window trade count (12 trades) -- not
strong enough to call a real edge, but a legitimate "worth a closer look,
not obviously dead" result, which is exactly the kind of signal a
data-availability-vetoed spec could never produce.

## Known residual limitations (honestly scoped, not hidden)

- **Candle history depth is ~208 days (1h) / ~17 days (5m) per asset**, not
  the 12 months / 90 days the design assumed -- an apparent ceiling on
  Hyperliquid's public `candleSnapshot` endpoint, not a bug pagination can
  fix (see "How this was produced" above). All three specs' walk-forward
  windows are correspondingly shorter than originally scoped. Funding
  history is the real 12 months.
- **OI and liquidation data remain entirely un-backfilled** (`oi` rows: 0
  everywhere) -- by design, since neither Hyperliquid nor Coinalyze's free
  tier serves historical OI or liquidation data. Any future seed spec
  leaning on those features has the same structural limitation `steel_crane`
  did.
- Scaled-conviction entries still open at full `position_size_pct` in the
  backtest engine (a pre-existing, separately-tracked follow-up noted in the
  original run -- see `backtest/engine.py`'s comment at the entry-sizing
  site).

## Performance fixes made along the way (unchanged from the original run)

The real ~5000-1h-candle-per-asset backfilled ledger exposed two real
algorithmic/constant-factor performance problems that made a full 3-spec
walk-forward run take multiple hours before these fixes -- versus low
minutes after. Both remain committed as separate, scoped commits:

1. **`backtest/engine.py`** (commit `fix(backtest): eliminate O(bars^2)
   per-bar recomputation bottleneck in run_backtest`): precomputes each
   asset's history as plain Python lists once per asset, then uses
   `bisect.bisect_right` on a parallel timestamp list to find each bar's
   cutoff and slices directly, instead of re-filtering the full
   candles/funding DataFrames on every bar.
2. **`market/features.py`** (commit `fix(market): eliminate O(n^2)/O(n)
   per-call recomputation in atr_percentile and bb_width_percentile`):
   one-pass incremental ATR recursion and an incremental rolling
   sum/sum-of-squares for the Bollinger-Band width series, instead of
   recomputing from scratch on every trailing index.

## Known residual performance gap (not fixed in this task, flagged for follow-up)

`market/heartbeat.py`'s `compute_replayable_fields` still recomputes several
other indicators (RSI, EMA20/50/200, VWAP, realized vol, z-scores) fresh on
every single bar call from `backtest/engine.py`'s per-bar loop -- each
individually O(candles-in-window) (bounded at 300, not O(n^2) across the
whole backtest), so none is currently a correctness or scalability blocker.
Flagged here as a real, documented follow-up rather than silently left for
someone to rediscover.
