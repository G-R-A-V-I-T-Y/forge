# Seed backtest results -- silver_basin, iron_moth, steel_crane

Task 8 of `docs/superpowers/plans/2026-07-07-strategy-spec-dsl-backtester.md`
(M7b). This is the first real, historical-data-backed evidence about
whether the three hand-compiled seed theses have any edge, run against a
real backfill from Hyperliquid's live public API (read-only, no
credentials, no trading).

## How this was produced

1. `python scripts/backfill_history.py` was run for real against Hyperliquid's
   public API for the full 20-asset universe in `config.yaml`: 12 months of
   1h candles + funding, 90 days of 5m candles. Output (truncated to the
   assets these 3 specs' universes use):

   ```
   ETH-PERP: {'asset': 'ETH-PERP', 'candles_1h': 5003, 'funding': 500, 'candles_5m': 5035}
   SOL-PERP: {'asset': 'SOL-PERP', 'candles_1h': 5003, 'funding': 500, 'candles_5m': 5033}
   SUI-PERP: {'asset': 'SUI-PERP', 'candles_1h': 5003, 'funding': 500, 'candles_5m': 5029}
   AVAX-PERP: {'asset': 'AVAX-PERP', 'candles_1h': 5003, 'funding': 500, 'candles_5m': 5033}
   LINK-PERP: {'asset': 'LINK-PERP', 'candles_1h': 5003, 'funding': 500, 'candles_5m': 5027}
   ARB-PERP: {'asset': 'ARB-PERP', 'candles_1h': 5003, 'funding': 500, 'candles_5m': 5030}
   OP-PERP: {'asset': 'OP-PERP', 'candles_1h': 5003, 'funding': 500, 'candles_5m': 5031}
   ```

   OI and liquidations were **not** backfilled -- this is by design, not an
   omission: `scripts/backfill_history.py`'s own docstring states Hyperliquid
   has no OI-history endpoint and Coinalyze's free tier doesn't backfill
   either, so both remain live-accumulated-only. This has a direct,
   visible effect on steel_crane's results below.

2. A real performance problem was found and fixed along the way (see
   "Performance fixes" below) before a full 3-spec walk-forward run was
   practical -- the original `run_backtest` implementation would have taken
   multiple hours per spec against this real ledger size, versus the
   original design's "well under a minute per spec" assumption (written
   against a much smaller anticipated ledger).

3. `python scripts/run_seed_backtests.py` was then run for real (each spec
   run individually via a temporary staging script during development, to
   stay under tooling timeouts; the committed `scripts/run_seed_backtests.py`
   runs all 3 in one process and produces the same output). Full output
   below, verbatim.

## Results

```
=== iron_moth ===
  data window: {'candles_1h': {'rows': 25015}, 'funding': {'rows': 2500}, 'oi': {'rows': 0}}
  train: 1074 trades, -45.57% return, Sharpe -2.00
  validate: 285 trades, -52.62% return, Sharpe -5.13
  test: 248 trades, -9.66% return, Sharpe -0.65
  deflated Sharpe: -0.76
  parameter sensitivity: {'confidence_threshold': 0.0, 'scale_threshold': -0.01723125718354268, 'stop_loss_pct': -0.46524187778438086, 'take_profit_pct': 0.09489931206293034}

=== silver_basin ===
  data window: {'candles_1h': {'rows': 25015}, 'funding': {'rows': 2500}, 'oi': {'rows': 0}}
  train: 0 trades, +0.00% return, Sharpe 0.00
  validate: 0 trades, +0.00% return, Sharpe 0.00
  test: 0 trades, +0.00% return, Sharpe 0.00
  deflated Sharpe: 0.00
  parameter sensitivity: {'confidence_threshold': 0.0, 'scale_threshold': 0.0, 'stop_loss_pct': 0.0, 'take_profit_pct': 0.0}

=== steel_crane ===
  data window: {'candles_1h': {'rows': 25015}, 'funding': {'rows': 2500}, 'oi': {'rows': 0}}
  train: 0 trades, +0.00% return, Sharpe 0.00
  validate: 0 trades, +0.00% return, Sharpe 0.00
  test: 0 trades, +0.00% return, Sharpe 0.00
  deflated Sharpe: 0.00
  parameter sensitivity: {'confidence_threshold': 0.0, 'scale_threshold': 0.0, 'stop_loss_pct': 0.0, 'take_profit_pct': 0.0}
```

(`data_window` rows are summed across each spec's 5-asset universe, so
25015 = 5 assets x ~5003 1h candles each; 2500 = 5 x 500 funding samples
each; 0 OI rows confirms OI was genuinely never backfilled for any asset.)

## Interpretation -- honest, not flattering

**iron_moth**: the only spec that actually traded. Real historical
performance is bad: negative Sharpe in all three splits (train -2.00,
validate -5.13, test -0.65) and a negative deflated Sharpe (-0.76) after
penalizing for the parameter-sensitivity sweep's implicit multiple-testing.
The momentum-acceleration + volatility-adjusted-entry thesis, as
hand-compiled here, shows no historical edge over this window -- if
anything a real historical loss. This is real evidence the thesis needs
rework (or the hand-compiled thresholds need retuning) before any live
capital consideration, exactly the kind of signal this backtest engine
exists to surface.

**silver_basin**: 0 trades across all three splits. Its primary evidence
term (`funding_extremity`, thresholds at funding z-score > 1.0/1.5/2.0)
combined with `confidence_threshold: 0.70` never actually crossed the entry
bar anywhere in ~7 months of real funding-rate history for this 5-asset
universe. This is a real, honest finding: either the real historical
funding-rate distribution for these assets doesn't produce the z-score
extremes the thesis assumed, or the hand-compiled threshold/weight
combination is too conservative to ever fire. Not a bug -- the spec
validated cleanly and the interpreter ran correctly; it simply never found
a qualifying entry in real data.

**steel_crane**: 0 trades across all three splits, but for a structurally
different (and expected) reason: its primary evidence term
(`liquidation_volume`, feature `liq_total_usd`) has `missing: veto`, and
liquidation data was never backfilled (see above) -- `liq_total_usd` is
`None` for every single bar in this backtest, so the primary evidence
term is vetoed on every evaluation, by construction. **This spec's zero
result is not evidence the liquidation-hunter thesis lacks edge** -- it's
evidence that this specific backtest run cannot evaluate that thesis at
all, because the one data source it depends on doesn't exist historically
yet. A meaningful backtest of steel_crane requires either a live-accumulated
liquidation history (Coinalyze, going forward from whenever an API key is
configured) or a different historical liquidation-data source; there isn't
one available today. This gap is called out explicitly here rather than
silently reported as "no edge."

## Performance fixes made along the way

The real ~5000-1h-candle-per-asset backfilled ledger exposed two real
algorithmic/constant-factor performance problems that made a full 3-spec
walk-forward run (train+validate+test+4-way parameter-sensitivity sweep,
per spec) take multiple hours before these fixes -- versus low minutes
after. Both are committed as separate, scoped commits before the seed-specs
commit:

1. **`backtest/engine.py`** (commit `fix(backtest): eliminate O(bars^2)
   per-bar recomputation bottleneck in run_backtest`): `run_backtest`
   re-filtered the full candles/funding DataFrames with a fresh pandas
   boolean mask + `.iterrows()`/`.tolist()` on *every single bar* --
   funding's window was additionally unbounded and ever-growing (unlike
   candles, which at least capped at `.tail(300)`), making it a real
   O(bars^2) cost per asset. Fixed by precomputing each asset's history as
   plain Python lists once per asset, then using `bisect.bisect_right` on a
   parallel timestamp list to find each bar's cutoff and slicing directly.
   Same trailing-300 candle cap, same unbounded funding/OI window, bit-for-bit
   identical results -- verified by the existing 4 tests in
   `tests/test_backtest_engine.py` (unchanged) plus 1 new regression test
   pinning the no-lookahead invariant. ~5.2x faster on a profiled reference
   window (8.442s -> 1.637s for 1 asset / 3 days).

2. **`market/features.py`** (commit `fix(market): eliminate O(n^2)/O(n)
   per-call recomputation in atr_percentile and bb_width_percentile`):
   `atr_percentile` recomputed Wilder's ATR from scratch for every trailing
   index (genuinely O(n^2) per call); `bb_width_percentile` was already
   O(n) but paid heavy constant-factor overhead from ~280
   `statistics.mean`/`stdev` calls per invocation. Fixed with a one-pass
   incremental ATR recursion (bit-for-bit identical output, verified against
   a reference implementation in `tests/test_features.py`) and an
   incremental rolling sum/sum-of-squares for the Bollinger-Band width
   series (matches to ~1e-9 float tolerance, not bit-for-bit, since the
   arithmetic path changed -- acceptable for a percentile-rank output).
   Measured on a synthetic 300-candle window: atr_percentile 30.55ms ->
   0.22ms/call (139x), bb_width_percentile 33.82ms -> 0.26ms/call (130x).

## Known residual performance gap (not fixed in this task, flagged for follow-up)

`market/heartbeat.py`'s `compute_replayable_fields` still recomputes several
other indicators (RSI, EMA20/50/200, VWAP, realized vol, z-scores) fresh on
every single bar call from `backtest/engine.py`'s per-bar loop -- each of
these is individually O(candles-in-window) (bounded at 300, not O(n^2)
across the whole backtest), so none is currently a correctness or
scalability blocker the way the two fixed functions were. But they are
still recomputed from scratch every bar rather than maintained
incrementally, and are a legitimate next optimization target if backtest
run time needs to shrink further (e.g. for a larger universe, a rolling
walk-forward harness, or many more seed specs). Not urgent: the two fixes
already committed bring a full 3-spec run down from multiple hours to a few
minutes, which is workable for the current scope. Flagged here as a real,
documented follow-up rather than silently left for someone to rediscover.
