# Strategy-Spec DSL & Backtest Engine — Design Spec

**Date:** 2026-07-07
**Status:** Approved, pending implementation
**Scope:** M7b per `docs/FORGE_PROPOSAL.md` — the strategy-spec DSL, its interpreter, the backtest engine (replay + walk-forward + overfit metrics), a one-time historical backfill, and 3 hand-compiled seed specs with known historical profiles. Excludes the event calendar and the statistical forecast feature (separate, sequenced sub-projects per the same milestone) and excludes wiring compiled specs into the live fast loop (M8's job).

## 1. Why this is the right shape

FORGE_PROPOSAL.md already settled the strategic question: not free code (unsafe, unverifiable), not a rigid config (kills expressiveness) — a constrained YAML DSL over the heartbeat's feature vocabulary, entry conditions as weighted evidence terms. This isn't a new invention: every thesis already written for this desk (`silver_basin`, `steel_crane`, `sage_turtle`, etc.) uses exactly one consistent shape — named evidence pillars, each a signed score contribution off a feature/threshold, summed into a composite confidence, gated by entry thresholds, with explicit per-pillar missing-data degradation (veto vs. skip-with-no-penalty vs. skip-with-uncertainty-penalty). The spec format is that shape, made machine-executable.

## 2. Spec schema (YAML)

```yaml
agent_id: sage_turtle
spec_version: 1
thesis_version: 1

universe:
  include: [FET-PERP, TAO-PERP, RENDER-PERP, ARB-PERP, OP-PERP, TIA-PERP]

regime_filter:
  exclude: [crisis]          # regime tags this spec refuses to trade; absent = all regimes allowed

entry:
  direction: short            # long | short | signal_determined (sign of weighted sum, for symmetric theses)
  confidence_threshold: 0.70  # >= this: full size. Below scale_threshold: no entry (firm veto).
  scale_threshold: 0.50       # between scale_threshold and confidence_threshold: size scaled by confidence
  evidence:
    - name: unlock_size_vs_float
      feature: unlock_size_pct_float
      thresholds:                        # evaluated in order, first match wins
        - {op: ">=", value: 0.03, weight: 0.7}
        - {op: ">=", value: 0.015, weight: 0.5}
        - {op: ">=", value: 0.005, weight: 0.2}
        - {op: "else", weight: 0.0}
      missing: veto                      # veto | skip (contribute 0, no penalty) | uncertainty:-0.1 (skip + flat penalty)
  secondary_evidence: []                 # identical shape, additive to the same composite score

exit:
  stop_loss_pct: 0.03
  take_profit_pct: 0.06
  max_hold_hours: 24

position:
  leverage: 3
  position_size_pct: 0.10
```

`op` supports `>`, `>=`, `<`, `<=`, `between` (`value: [lo, hi]`), `==`, and the terminal `else`. `feature` names reference the replayable feature vocabulary (§4) — the same names the live heartbeat packet already uses (`funding_zscore`, `oi_zscore`, `atr_percentile`, ...), plus any names the event-calendar sub-project adds later (`days_to_next_unlock`, etc. — those evidence pillars simply have no data and hit their `missing` rule until that sub-project ships).

## 3. Components

- **`backtest/dsl.py`** — the schema as typed dataclasses, YAML load/dump.
- **`backtest/validator.py`** — every referenced `feature` name exists in the known vocabulary; thresholds within one evidence term are internally ordered and end in `else`; `scale_threshold <= confidence_threshold`; `position_size_pct`/`leverage` within `risk/gate.py`'s desk-wide caps (a spec that would fail the live risk gate is rejected at compile time, not discovered at backtest time).
- **`backtest/interpreter.py`** — `evaluate(spec, feature_row: dict) -> Decision` — sums evidence weights (applying each term's `missing` rule when the row lacks that feature), compares against `confidence_threshold`/`scale_threshold`, returns the same `{action, confidence, evidence_strength, ...}` shape `agents/decision_loop.py` already produces from an LLM call. This is deliberately the same output contract — a compiled agent (M8) will eventually call this instead of an LLM, but wiring that swap is out of scope here.
- **`backtest/engine.py`** — replay: for each bar in the spec's date range and `universe`, build a `feature_row` (§4) and call the interpreter; simulate a fill using the fee model shared with paper (`taker_fee` from config) and a **conservative fixed slippage assumption** rather than `execute_close`'s live `slippage_estimate` — the ledger doesn't capture order-book depth (deliberately; that data was the retired microstructure paradigm), so a live-quality slippage estimate doesn't exist historically. Document this gap plainly in every backtest report rather than pretending parity with paper fills.
- **`backtest/walk_forward.py`** — single 70/15/15 train/validate/test split (not rolling — real history depth varies too much by feature to justify a rolling harness yet); deflated Sharpe on the test window; a parameter-sensitivity sweep that perturbs each threshold/weight by ±20% and reports how much the test-window Sharpe moves (a spec whose edge evaporates under small perturbations is overfit, regardless of its raw backtest number).

## 4. Feature computation: reuse, don't reimplement

`market/features.py`'s `FEATURE_REGISTRY` functions are already pure (no I/O, no live state) — directly reusable by the backtester as-is. The gap is `market/heartbeat.py`'s `_compute_asset_fields`, which currently mixes two kinds of computation in one function:

- **Replayable** (derivable from `candles_5m`/`funding`/`oi` alone — exactly what the ledger stores): returns, EMA/ATR/RSI, realized vol, VWAP distance, funding/OI z-scores and acceleration, regime-relevant aggregates.
- **Live-only** (needs order-book depth or the trade tape — never in the ledger, by design): `bid_depth`/`ask_depth`/`depth_imbalance`, `slippage_estimate`, `buy_volume`/`sell_volume`/`aggressor_ratio`, `avg_trade_size`, `large_trade_volume_usd`.

Splitting `_compute_asset_fields` into `compute_replayable_fields(candles, funding_history, oi_history) -> dict` (called by both the live heartbeat and the backtester) plus the live-only remainder (called only by the live heartbeat) is a real, explicit task — not a detail to wave at implementation time — because it's what makes "the interpreter sees the same features live and in backtest" true rather than aspirational. Live-only fields are simply absent from a backtest `feature_row`; any evidence term referencing one always hits its `missing` rule, which is honest (a spec leaning on microstructure evidence backtests as permanently degraded — which is the correct signal, matching the recommendation in `docs/STRATEGIC_ASSESSMENT_2026-07-04.md` to retire that paradigm).

## 5. Historical backfill — not a full year at 5-minute resolution

Backfilling a full year of 5-minute candles is unnecessary and works against the ledger's own design: `candles_5m` already resolution-decays to hourly beyond a 12-month window (`scripts/compact_ledger.py`), so backfilling a year at 5m only to immediately need it compacted is wasted work. The actual plan, matching the original (pre-ledger-redesign) M7 scoping:

| Stream | Backfill window | Rationale |
|---|---|---|
| `candles_1h` | 12 months | The walk-forward backbone |
| `funding` | 12 months | `get_funding_history(asset, start_time_ms)` already accepts an arbitrary historical start — no client change needed |
| `candles_5m` | 90 days | Recent-precision only (exact SL/TP trigger sequencing, matching `store/positions.py`'s `find_first_cross`) |
| `oi` | none — live-accumulated only | Hyperliquid has no OI history endpoint; not retroactively available, already the documented ledger limitation |
| `liquidations` | none — live-accumulated only | Same reasoning; Coinalyze's free tier doesn't backfill either |

`market/hyperliquid.py`'s `get_ohlcv(asset, interval, lookback_candles)` needs a small extension to accept an explicit historical `start_ms`/`end_ms` (currently always relative to now) — the underlying `candleSnapshot` API call already supports arbitrary ranges. Backfilled rows land directly in the ledger's normal partitioned format (`ledger/candles_1h/{YYYY-MM}.jsonl`, etc.) via `append_ledger_record`, dated by the historical candle's own timestamp, not "now" — so they compact and decay through the exact same monthly pipeline as organically-captured data, no special-casing.

A genuine, honestly-reported limitation: OI- and liquidation-dependent specs (`copper_vane`, `steel_crane`) will have a much shorter real backtest window than funding/price-driven ones (`silver_basin`, `iron_moth`) for a long time. The backtest report states the actual data window used per spec rather than implying a false year-long validation.

## 6. The z-score window bug

`market/heartbeat.py` fetches funding history with `start_lookback_ms = now_ms - LOOKBACK_HOURS * 3600 * 1000` where `LOOKBACK_HOURS = 25` — correct for the price-candle EMA200 lookback it was designed for, but every thesis (`silver_basin`'s "z-score vs 14-day history" being the clearest example) assumes a 14-day funding baseline. This is a parameter bug, not a data-availability problem — Hyperliquid's `fundingHistory` endpoint already serves arbitrary past windows going back further than 25 hours today. Fix: fetch funding history with its own independent lookback (14 days = 336 hours), decoupled from the candle lookback.

## 7. Seed specs and validation

Hand-compile 3 specs directly from existing thesis prose: `silver_basin` (funding dislocation — mostly funding-driven, gets the full 12-month backtest window), `iron_moth` (cross-sectional momentum — price-driven, full window), `steel_crane` (liquidation hunter — OI/liquidation-driven, honestly short window). Backtest all 3 through the walk-forward harness. This is the first honest evidence about whether these theses have any historical edge — a spec that shows no edge on 12 months of real data is exactly the signal M8's evolution loop needs, not a failure of this milestone.

## 8. Out of scope (separate, already-sequenced work)

- Event calendar (`ledger/events.jsonl`, token unlocks, macro calendar) — immediate next sub-project after this one.
- Statistical/Bayesian forecast feature — deferred per FORGE_PROPOSAL's own guidance until real history spans months, not days.
- Wiring a validated spec into the live fast loop as a compiled (non-LLM) agent — M8's "convert the desk" task.
- Liquidation feed itself — already done (`market/coinalyze.py`, M7a).
