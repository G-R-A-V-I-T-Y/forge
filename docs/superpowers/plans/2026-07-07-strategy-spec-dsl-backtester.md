# Strategy-Spec DSL & Backtest Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the strategy-spec DSL, its interpreter, and a backtest engine (replay + walk-forward + overfit metrics) that lets a hand-compiled spec be validated against real history in under a minute — M7b per `docs/FORGE_PROPOSAL.md`.

**Architecture:** An evidence-weighted YAML DSL mirrors the shape every thesis already uses. The interpreter evaluates a spec against a feature-vector dict and returns the same `{action, confidence, evidence_strength}` shape an LLM decision already produces. The backtest engine replays historical ledger data through a *reused* feature-computation core (never a separate reimplementation) so backtest and live see identical features. A one-time backfill gets 12 months of candles/funding (90 days for 5m) into the ledger first, since the ledger itself has only days of organic history so far.

**Tech Stack:** Python 3.11, PyYAML (new dependency), pandas/pyarrow (already present), pytest.

## Global Constraints

- Python interpreter: `C:\ProgramData\Anaconda3\python.exe`
- Test command: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py`
- No behavior change to any existing live code path unless a task explicitly says so — `_compute_asset_fields`'s live output must be byte-for-byte identical after Task 2's refactor.
- Every new ledger-facing default parameter must resolve at call time, not be bound at function-definition time (the `ledger_dir: str = LEDGER_DIR` anti-pattern found and fixed three times in the M7a plan) — irrelevant to most of this plan's new code, but Task 5's backfill script writes to the ledger and must follow `store/ledger.py`'s existing `append_ledger_record` contract exactly, never introduce its own bound default.
- Design reference: `docs/superpowers/specs/2026-07-07-strategy-spec-dsl-backtester-design.md`.

---

### Task 1: Fix the funding z-score lookback window bug

**Files:**
- Modify: `market/heartbeat.py`
- Test: `tests/test_heartbeat.py`

**Interfaces:**
- Produces: `FUNDING_LOOKBACK_HOURS = 14 * 24` module constant (336), used only for the funding-history fetch, independent of `LOOKBACK_HOURS` (25, still used for the candle/EMA200 fetch).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_heartbeat.py`:

```python
def test_fetch_asset_snapshot_uses_14_day_funding_lookback(monkeypatch):
    """silver_basin's thesis assumes a 14-day funding z-score baseline;
    the fetch must request that window, not the 25h candle lookback."""
    import asyncio
    from market.heartbeat import _fetch_asset_snapshot, FUNDING_LOOKBACK_HOURS

    assert FUNDING_LOOKBACK_HOURS == 14 * 24

    captured = {}

    class StubProvider:
        async def get_ohlcv(self, asset, interval, lookback):
            return []

        async def get_funding_history(self, asset, start_time_ms):
            captured["start_time_ms"] = start_time_ms
            return []

        async def get_open_interest(self, asset):
            return {}

        async def get_funding_rate(self, asset):
            return {}

        async def get_orderbook(self, asset, depth=5):
            return {"bids": [], "asks": []}

        async def get_recent_trades(self, asset, hours=1):
            return []

    asyncio.run(_fetch_asset_snapshot(StubProvider(), "BTC-PERP"))

    import time
    now_ms = int(time.time() * 1000)
    expected_start = now_ms - FUNDING_LOOKBACK_HOURS * 3600 * 1000
    # allow a few seconds of test-execution drift
    assert abs(captured["start_time_ms"] - expected_start) < 10_000
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_heartbeat.py::test_fetch_asset_snapshot_uses_14_day_funding_lookback -v`
Expected: FAIL — `ImportError: cannot import name 'FUNDING_LOOKBACK_HOURS'` (or the captured start_time_ms matches the old 25h window instead).

- [ ] **Step 3: Implement**

In `market/heartbeat.py`, add the new constant next to the existing lookback constants (near line 53):

```python
LOOKBACK_CANDLES = 300
LOOKBACK_HOURS = 25

# Funding z-score baseline: every thesis (silver_basin's "z-score vs 14-day
# history" is the clearest example) assumes 14 days, independent of the 25h
# candle/EMA200 lookback above -- these were incorrectly sharing one window.
FUNDING_LOOKBACK_HOURS = 14 * 24
```

Then in `_fetch_asset_snapshot` (around line 328-329), replace:

```python
    now_ms = int(time.time() * 1000)
    start_lookback_ms = now_ms - LOOKBACK_HOURS * 3600 * 1000

    candles, funding_history, oi, funding, book, trades = await asyncio.gather(
        _safe(lambda: provider.get_ohlcv(asset, "5m", LOOKBACK_CANDLES), []),
        _safe(lambda: provider.get_funding_history(asset, start_lookback_ms), []),
```

with:

```python
    now_ms = int(time.time() * 1000)
    candle_start_ms = now_ms - LOOKBACK_HOURS * 3600 * 1000
    funding_start_ms = now_ms - FUNDING_LOOKBACK_HOURS * 3600 * 1000

    candles, funding_history, oi, funding, book, trades = await asyncio.gather(
        _safe(lambda: provider.get_ohlcv(asset, "5m", LOOKBACK_CANDLES), []),
        _safe(lambda: provider.get_funding_history(asset, funding_start_ms), []),
```

(`candle_start_ms` replaces the old `start_lookback_ms` purely for clarity that it's candle-specific now; it is not used elsewhere in the function, so renaming it is safe.)

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_heartbeat.py -v`
Expected: all pass, including the new test.

- [ ] **Step 5: Commit**

```bash
git add market/heartbeat.py tests/test_heartbeat.py
git commit -m "fix(backtest): fetch funding history with an independent 14-day lookback"
```

**Definition of done:** funding z-score baselines are computed against a 14-day window, matching every thesis's stated assumption, independent of the unrelated 25h candle lookback.

---

### Task 2: Split `_compute_asset_fields` into a replayable core + live-only extension

**Files:**
- Modify: `market/heartbeat.py`
- Test: `tests/test_heartbeat.py`

**Interfaces:**
- Produces: `compute_replayable_fields(candles, funding_history, oi_val, funding_val, prior_oi_history, liq_data=None) -> dict` — every field derivable from candles/funding/OI/liquidations alone (the subset the ledger stores), including every `FEATURE_REGISTRY` feature. This is what Task 6's backtest engine calls directly.
- Consumed internally by `_compute_asset_fields`, which now calls `compute_replayable_fields` + a new `_compute_live_only_fields` and merges them — **output must be identical to before this refactor** for every existing test.

- [ ] **Step 1: Write the failing test proving the split**

Add to `tests/test_heartbeat.py`:

```python
def test_compute_replayable_fields_excludes_live_only_fields():
    """The replayable core must never require order-book or trade-tape data
    -- those are the fields a historical backtest can never have."""
    from market.heartbeat import compute_replayable_fields

    candles = [[i * 300_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0] for i in range(250)]
    result = compute_replayable_fields(
        candles=candles,
        funding_history=[{"time": 0, "fundingRate": 0.0001}],
        oi_val=1_000_000.0,
        funding_val=0.0002,
        prior_oi_history=[900_000.0, 950_000.0],
        liq_data={"liq_total_usd": 50_000.0, "liq_long_usd": 30_000.0, "liq_short_usd": 20_000.0},
    )

    for live_only_key in ("spread", "bid_depth", "ask_depth", "depth_imbalance",
                           "slippage_estimate", "buy_volume", "sell_volume",
                           "aggressor_ratio", "avg_trade_size", "largest_trade"):
        assert live_only_key not in result, f"{live_only_key} is live-only, must not appear in replayable fields"

    for replayable_key in ("price", "return_5m", "atr", "rsi", "ema20", "funding_zscore",
                            "oi_zscore", "oi_drawdown_pct", "liquidation_cascade_flag",
                            "momentum_acceleration", "atr_percentile", "liq_total_usd"):
        assert replayable_key in result, f"{replayable_key} should be computed from replayable inputs"


def test_compute_asset_fields_unchanged_after_refactor(monkeypatch):
    """Full live output must be byte-for-byte identical to before the split --
    this is a regression guard, not a spec of new behavior."""
    from market.heartbeat import _compute_asset_fields, PER_ASSET_FIELDS

    candles = [[i * 300_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0] for i in range(250)]
    raw = {
        "candles": candles,
        "funding_history": [{"time": 0, "fundingRate": 0.0001}],
        "oi": {"openInterest": 1_000_000.0},
        "funding": {"fundingRate": 0.0002},
        "book": {"bids": [[99.5, 5.0]], "asks": [[100.5, 5.0]]},
        "trades": [{"size": 1.0, "price": 100.0, "side": "B"}],
    }
    result = _compute_asset_fields(raw, prior_oi_history=[900_000.0, 950_000.0])

    assert set(result.keys()) == set(PER_ASSET_FIELDS)
    assert result["spread"] is not None  # live-only field still present via merge
    assert result["funding_zscore"] is not None  # replayable field still present via merge
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_heartbeat.py -k "replayable or unchanged_after_refactor" -v`
Expected: FAIL — `ImportError: cannot import name 'compute_replayable_fields'`.

- [ ] **Step 3: Implement the split**

In `market/heartbeat.py`, replace the entire `_compute_asset_fields` function (currently lines 349-524, from `def _compute_asset_fields(` through the final `return result`) with:

```python
def compute_replayable_fields(
    candles: list[list],
    funding_history: list[dict],
    oi_val: float | None,
    funding_val: float | None,
    prior_oi_history: list[float],
    liq_data: dict[str, float | None] | None = None,
) -> dict:
    """Every field derivable purely from candles/funding/OI/liquidations --
    exactly the subset store/ledger.py's ledger stores (candles_5m, funding,
    oi, liquidations). This is the ONLY set of fields a historical backtest
    can ever compute, since the ledger deliberately never captures order-book
    depth or the trade tape (the retired microstructure paradigm -- see
    docs/STRATEGIC_ASSESSMENT_2026-07-04.md). Used both by the live heartbeat
    (via _compute_asset_fields below) and by backtest/engine.py.
    """
    if not candles:
        return {}

    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    volumes = [c[5] for c in candles]

    price = closes[-1]
    volume = volumes[-1]

    return_5m = _pct_return(closes, 1)
    return_30m = _pct_return(closes, 6)
    return_4h = _pct_return(closes, 48)
    return_24h = _pct_return(closes, 288)

    atr = _atr(highs, lows, closes, 14)
    rsi = _rsi(closes, 14)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    realized_vol = _realized_vol(closes)
    vwap = _vwap(candles)
    vwap_distance = (price - vwap) / vwap if vwap else None

    volume_zscore = _zscore(volume, volumes[:-1]) if len(volumes) > 1 else None

    funding_history_vals = [
        f.get("fundingRate") for f in funding_history if f.get("fundingRate") is not None
    ]
    funding_zscore = _zscore(funding_val, funding_history_vals)

    oi_zscore = _zscore(oi_val, prior_oi_history)

    oi_prior = prior_oi_history[-1] if prior_oi_history else None
    oi_drawdown_pct = (
        (oi_val - oi_prior) / oi_prior
        if oi_val is not None and oi_prior is not None and oi_prior != 0
        else None
    )

    liquidation_cascade_flag = (
        1
        if (oi_drawdown_pct is not None
            and oi_drawdown_pct < -0.03
            and volume_zscore is not None
            and volume_zscore > 1.5
            and abs(return_5m or 0) > 0.015)
        else 0
    )

    candles_5m = candles
    candles_30m = _resample_candles(candles, RESAMPLE_FACTOR_30M)
    candles_4h = _resample_candles(candles, RESAMPLE_FACTOR_4H)

    result = {
        "price": price,
        "return_5m": return_5m,
        "return_30m": return_30m,
        "return_4h": return_4h,
        "return_24h": return_24h,
        "volume": volume,
        "open_interest": oi_val,
        "funding": funding_val,
        "atr": atr,
        "realized_vol": realized_vol,
        "rsi": rsi,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "vwap_distance": vwap_distance,
        "volume_zscore": volume_zscore,
        "funding_zscore": funding_zscore,
        "oi_zscore": oi_zscore,
        "oi_drawdown_pct": oi_drawdown_pct,
        "liquidation_cascade_flag": liquidation_cascade_flag,
        "candles_5m": candles_5m,
        "candles_30m": candles_30m,
        "candles_4h": candles_4h,
        "liq_total_usd": liq_data.get("liq_total_usd") if liq_data else None,
        "liq_long_usd": liq_data.get("liq_long_usd") if liq_data else None,
        "liq_short_usd": liq_data.get("liq_short_usd") if liq_data else None,
    }

    # raw_data shape FEATURE_REGISTRY functions expect: only the replayable
    # inputs they were ever documented to need (funding_history for
    # funding_acceleration; nothing here needs book/trades).
    raw_data_for_features = {"funding_history": funding_history}
    for feature_name, feature_fn in FEATURE_REGISTRY.items():
        try:
            result[feature_name] = feature_fn(
                candles=candles, closes=closes, highs=highs,
                lows=lows, volumes=volumes, fields=result,
                raw_data=raw_data_for_features,
            )
        except Exception:
            result[feature_name] = None

    return result


def _compute_live_only_fields(raw: dict, price: float) -> dict:
    """Order-book and trade-tape fields -- never available to a backtest,
    since the ledger deliberately never captures this data (retired
    microstructure paradigm). Only the live heartbeat calls this."""
    book = raw["book"]
    bids = (book.get("bids") or [])[:5]
    asks = (book.get("asks") or [])[:5]
    bid_depth = sum(sz for _px, sz in bids) if bids else 0.0
    ask_depth = sum(sz for _px, sz in asks) if asks else 0.0
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else price
    spread = (
        (best_ask - best_bid) / mid
        if best_bid is not None and best_ask is not None and mid
        else None
    )
    depth_imbalance = (
        (bid_depth - ask_depth) / (bid_depth + ask_depth)
        if (bid_depth + ask_depth) > 0
        else None
    )
    top5_imbalance = depth_imbalance

    slippage_estimate = _slippage_estimate(asks, mid, REFERENCE_NOTIONAL_USD)

    trades = raw["trades"]
    buy_volume = sum(t["size"] for t in trades if _is_buy(t.get("side")))
    sell_volume = sum(t["size"] for t in trades if not _is_buy(t.get("side")))
    total_vol = buy_volume + sell_volume
    aggressor_ratio = buy_volume / total_vol if total_vol > 0 else 0.5
    avg_trade_size = statistics.mean([t["size"] for t in trades]) if trades else None
    largest_trade = max((t["size"] * t["price"] for t in trades), default=None)

    large_trade_threshold = (avg_trade_size or 0) * 3
    large_trade_volume_usd = (
        sum(t["size"] * t["price"] for t in trades if t["size"] > large_trade_threshold)
        if trades and avg_trade_size and large_trade_threshold > 0
        else None
    )

    return {
        "spread": spread,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "depth_imbalance": depth_imbalance,
        "top5_imbalance": top5_imbalance,
        "slippage_estimate": slippage_estimate,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "aggressor_ratio": aggressor_ratio,
        "avg_trade_size": avg_trade_size,
        "largest_trade": largest_trade,
        "large_trade_volume_usd": large_trade_volume_usd,
    }


def _compute_asset_fields(
    raw: dict,
    prior_oi_history: list[float],
    liq_data: dict[str, float | None] | None = None,
) -> dict:
    """Live entry point: replayable + live-only fields merged. Output shape
    is unchanged from before this function was split -- see
    test_compute_asset_fields_unchanged_after_refactor."""
    candles = raw["candles"]
    if not candles:
        return {k: None for k in PER_ASSET_FIELDS}

    oi_val = raw["oi"].get("openInterest")
    funding_val = raw["funding"].get("fundingRate")

    replayable = compute_replayable_fields(
        candles, raw["funding_history"], oi_val, funding_val, prior_oi_history, liq_data,
    )
    live_only = _compute_live_only_fields(raw, replayable["price"])

    return {**replayable, **live_only}
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_heartbeat.py tests/test_heartbeat_ledger.py -v`
Expected: all pass, including both new tests. Pay particular attention to `test_compute_asset_fields_unchanged_after_refactor` and every pre-existing heartbeat test — a single field mismatch means the refactor changed live behavior, which this task must not do.

- [ ] **Step 5: Commit**

```bash
git add market/heartbeat.py tests/test_heartbeat.py
git commit -m "refactor(backtest): split _compute_asset_fields into a replayable core + live-only extension"
```

**Definition of done:** `compute_replayable_fields` produces every field a historical backtest can compute from ledger data alone, with zero dependency on order-book or trade-tape data; `_compute_asset_fields`'s live output is unchanged.

---

### Task 3: Strategy-spec DSL schema + validator

**Files:**
- Create: `backtest/__init__.py` (empty)
- Create: `backtest/dsl.py`
- Create: `backtest/validator.py`
- Test: `tests/test_dsl.py`, `tests/test_validator.py`

**Interfaces:**
- Produces: `load_spec(path: str) -> Spec` (dataclass), `Spec`, `EvidenceTerm`, `Threshold` dataclasses (`backtest/dsl.py`).
- Produces: `validate_spec(spec: Spec, config: dict) -> list[str]` — returns a list of human-readable error strings (empty list = valid) (`backtest/validator.py`). `config` is the desk config dict (`max_leverage`, `max_position_size_pct`, `max_concurrent_positions`) from `config.yaml`, used to cross-check against `risk/gate.py`'s caps.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dsl.py`:

```python
import pytest

from backtest.dsl import load_spec, Spec


SAMPLE_SPEC_YAML = """
agent_id: sage_turtle
spec_version: 1
thesis_version: 1

universe:
  include: [FET-PERP, TAO-PERP]

regime_filter:
  exclude: [crisis]

entry:
  direction: short
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: unlock_size_vs_float
      feature: unlock_size_pct_float
      thresholds:
        - {op: ">=", value: 0.03, weight: 0.7}
        - {op: ">=", value: 0.015, weight: 0.5}
        - {op: "else", weight: 0.0}
      missing: veto
  secondary_evidence: []

exit:
  stop_loss_pct: 0.03
  take_profit_pct: 0.06
  max_hold_hours: 24

position:
  leverage: 3
  position_size_pct: 0.10
"""


def test_load_spec_parses_all_fields(tmp_path):
    path = tmp_path / "sage_turtle_v1.yaml"
    path.write_text(SAMPLE_SPEC_YAML, encoding="utf-8")

    spec = load_spec(str(path))

    assert isinstance(spec, Spec)
    assert spec.agent_id == "sage_turtle"
    assert spec.spec_version == 1
    assert spec.universe_include == ["FET-PERP", "TAO-PERP"]
    assert spec.regime_exclude == ["crisis"]
    assert spec.direction == "short"
    assert spec.confidence_threshold == 0.70
    assert spec.scale_threshold == 0.50
    assert len(spec.evidence) == 1
    assert spec.evidence[0].name == "unlock_size_vs_float"
    assert spec.evidence[0].feature == "unlock_size_pct_float"
    assert spec.evidence[0].missing == "veto"
    assert len(spec.evidence[0].thresholds) == 3
    assert spec.evidence[0].thresholds[0].op == ">="
    assert spec.evidence[0].thresholds[0].value == 0.03
    assert spec.evidence[0].thresholds[0].weight == 0.7
    assert spec.evidence[0].thresholds[-1].op == "else"
    assert spec.stop_loss_pct == 0.03
    assert spec.take_profit_pct == 0.06
    assert spec.max_hold_hours == 24
    assert spec.leverage == 3
    assert spec.position_size_pct == 0.10


def test_load_spec_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_spec(str(tmp_path / "nope.yaml"))
```

Create `tests/test_validator.py`:

```python
from backtest.dsl import load_spec
from backtest.validator import validate_spec

VALID_YAML = """
agent_id: test_agent
spec_version: 1
thesis_version: 1
universe: {include: [BTC-PERP]}
regime_filter: {exclude: []}
entry:
  direction: long
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: funding_extreme
      feature: funding_zscore
      thresholds:
        - {op: ">", value: 2.0, weight: 0.7}
        - {op: "else", weight: 0.0}
      missing: veto
  secondary_evidence: []
exit: {stop_loss_pct: 0.02, take_profit_pct: 0.04, max_hold_hours: 8}
position: {leverage: 4, position_size_pct: 0.10}
"""

CONFIG = {"max_leverage": 10, "max_position_size_pct": 0.20, "max_concurrent_positions": 3}


def _spec_from(yaml_text, tmp_path, name="spec.yaml"):
    path = tmp_path / name
    path.write_text(yaml_text, encoding="utf-8")
    return load_spec(str(path))


def test_valid_spec_has_no_errors(tmp_path):
    spec = _spec_from(VALID_YAML, tmp_path)
    assert validate_spec(spec, CONFIG) == []


def test_unknown_feature_name_is_rejected(tmp_path):
    bad = VALID_YAML.replace("funding_zscore", "not_a_real_feature")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("not_a_real_feature" in e for e in errors)


def test_thresholds_must_end_in_else(tmp_path):
    bad = VALID_YAML.replace(
        '- {op: "else", weight: 0.0}\n', ''
    )
    spec = _spec_from(bad, tmp_path, name="bad.yaml")
    errors = validate_spec(spec, CONFIG)
    assert any("else" in e for e in errors)


def test_scale_threshold_must_not_exceed_confidence_threshold(tmp_path):
    bad = VALID_YAML.replace("scale_threshold: 0.50", "scale_threshold: 0.90")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("scale_threshold" in e for e in errors)


def test_leverage_exceeding_desk_cap_is_rejected(tmp_path):
    bad = VALID_YAML.replace("leverage: 4", "leverage: 15")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("leverage" in e for e in errors)


def test_position_size_exceeding_desk_cap_is_rejected(tmp_path):
    bad = VALID_YAML.replace("position_size_pct: 0.10", "position_size_pct: 0.50")
    spec = _spec_from(bad, tmp_path)
    errors = validate_spec(spec, CONFIG)
    assert any("position_size_pct" in e for e in errors)
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_dsl.py tests/test_validator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest'`.

- [ ] **Step 3: Implement `backtest/dsl.py`**

Create `backtest/__init__.py` (empty file).

Create `backtest/dsl.py`:

```python
"""backtest/dsl.py -- the strategy-spec DSL: schema + YAML loader.

An evidence-weighted YAML format matching the shape every thesis already
uses (see docs/superpowers/specs/2026-07-07-strategy-spec-dsl-backtester-design.md
section 2 for the full field reference). Not free code (unsafe, unverifiable)
and not a rigid config (kills expressiveness) -- entry conditions as weighted
evidence terms over the same feature vocabulary market/heartbeat.py's
replayable core produces.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml


@dataclass(frozen=True)
class Threshold:
    op: str  # ">", ">=", "<", "<=", "between", "==", "else"
    weight: float
    value: float | list[float] | None = None


@dataclass(frozen=True)
class EvidenceTerm:
    name: str
    feature: str
    thresholds: list[Threshold]
    missing: str  # "veto" | "skip" | "uncertainty:-0.1" (a skip with a flat penalty)


@dataclass(frozen=True)
class Spec:
    agent_id: str
    spec_version: int
    thesis_version: int
    universe_include: list[str]
    regime_exclude: list[str]
    direction: str  # "long" | "short" | "signal_determined"
    confidence_threshold: float
    scale_threshold: float
    evidence: list[EvidenceTerm]
    secondary_evidence: list[EvidenceTerm]
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_hours: float
    leverage: int
    position_size_pct: float


def _parse_threshold(raw: dict) -> Threshold:
    return Threshold(op=raw["op"], weight=raw["weight"], value=raw.get("value"))


def _parse_evidence_term(raw: dict) -> EvidenceTerm:
    return EvidenceTerm(
        name=raw["name"],
        feature=raw["feature"],
        thresholds=[_parse_threshold(t) for t in raw["thresholds"]],
        missing=raw["missing"],
    )


def load_spec(path: str) -> Spec:
    """Parse a spec YAML file into a Spec. Raises FileNotFoundError if the
    path doesn't exist; raises KeyError with the missing field name if the
    YAML is missing a required key (fail loud -- a malformed spec must never
    silently produce a partially-valid Spec)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    entry = raw["entry"]
    exit_ = raw["exit"]
    position = raw["position"]

    return Spec(
        agent_id=raw["agent_id"],
        spec_version=raw["spec_version"],
        thesis_version=raw["thesis_version"],
        universe_include=raw["universe"]["include"],
        regime_exclude=raw.get("regime_filter", {}).get("exclude", []),
        direction=entry["direction"],
        confidence_threshold=entry["confidence_threshold"],
        scale_threshold=entry["scale_threshold"],
        evidence=[_parse_evidence_term(e) for e in entry["evidence"]],
        secondary_evidence=[_parse_evidence_term(e) for e in entry.get("secondary_evidence", [])],
        stop_loss_pct=exit_["stop_loss_pct"],
        take_profit_pct=exit_["take_profit_pct"],
        max_hold_hours=exit_["max_hold_hours"],
        leverage=position["leverage"],
        position_size_pct=position["position_size_pct"],
    )
```

- [ ] **Step 4: Run dsl tests to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_dsl.py -v`
Expected: 2 passed.

- [ ] **Step 5: Implement `backtest/validator.py`**

The known replayable feature vocabulary is `market/heartbeat.py`'s `PER_ASSET_FIELDS` (minus the live-only fields Task 2 excluded from `compute_replayable_fields`) plus any `FEATURE_REGISTRY` name. Rather than hand-maintain a duplicate list, derive it directly from those two sources so the validator never drifts from what the interpreter can actually evaluate.

Create `backtest/validator.py`:

```python
"""backtest/validator.py -- semantic validation for a loaded Spec.

Structural validation (required fields, types) happens in dsl.py's
load_spec via YAML/dataclass construction, which raises on malformed input.
This module checks the SEMANTIC rules a structurally-valid spec can still
violate: referencing a feature the interpreter can never compute, an
internally-inconsistent threshold list, or position parameters that would
fail risk/gate.py's live checks (rejected here at compile time instead of
discovered at backtest or live-trading time).
"""
from __future__ import annotations

from backtest.dsl import EvidenceTerm, Spec
from market.features import FEATURE_REGISTRY

# Fields compute_replayable_fields() always produces (market/heartbeat.py),
# excluding the live-only fields that function deliberately never computes.
# Kept in sync by hand with market/heartbeat.py's PER_ASSET_FIELDS minus the
# live-only set _compute_live_only_fields() returns -- both lists are small
# and stable, and a mismatch here is caught immediately by
# test_unknown_feature_name_is_rejected-style tests against real specs.
REPLAYABLE_FEATURES = {
    "price", "return_5m", "return_30m", "return_4h", "return_24h", "volume",
    "open_interest", "funding", "atr", "realized_vol", "rsi",
    "ema20", "ema50", "ema200", "vwap_distance", "volume_zscore",
    "funding_zscore", "oi_zscore", "oi_drawdown_pct", "liquidation_cascade_flag",
    "liq_total_usd", "liq_long_usd", "liq_short_usd",
} | set(FEATURE_REGISTRY.keys())

VALID_OPS = {">", ">=", "<", "<=", "between", "==", "else"}


def _validate_evidence_term(term: EvidenceTerm, label: str) -> list[str]:
    errors = []
    if term.feature not in REPLAYABLE_FEATURES:
        errors.append(
            f"{label} '{term.name}': feature '{term.feature}' is not in the "
            f"replayable feature vocabulary (not computable from ledger data)"
        )
    if not term.thresholds or term.thresholds[-1].op != "else":
        errors.append(f"{label} '{term.name}': thresholds must end with an 'else' catch-all")
    for t in term.thresholds:
        if t.op not in VALID_OPS:
            errors.append(f"{label} '{term.name}': unknown threshold op '{t.op}'")
        if t.op == "between" and (not isinstance(t.value, list) or len(t.value) != 2):
            errors.append(f"{label} '{term.name}': 'between' requires value: [lo, hi]")
    if term.missing not in ("veto", "skip") and not term.missing.startswith("uncertainty:"):
        errors.append(
            f"{label} '{term.name}': missing rule '{term.missing}' must be "
            f"'veto', 'skip', or 'uncertainty:-N'"
        )
    return errors


def validate_spec(spec: Spec, config: dict) -> list[str]:
    """Returns a list of human-readable error strings; empty = valid."""
    errors: list[str] = []

    if spec.direction not in ("long", "short", "signal_determined"):
        errors.append(f"direction '{spec.direction}' must be 'long', 'short', or 'signal_determined'")

    if spec.scale_threshold > spec.confidence_threshold:
        errors.append(
            f"scale_threshold ({spec.scale_threshold}) must not exceed "
            f"confidence_threshold ({spec.confidence_threshold})"
        )

    if not spec.evidence:
        errors.append("entry.evidence must have at least one term")

    for term in spec.evidence:
        errors.extend(_validate_evidence_term(term, "evidence"))
    for term in spec.secondary_evidence:
        errors.extend(_validate_evidence_term(term, "secondary_evidence"))

    if spec.leverage > config["max_leverage"]:
        errors.append(f"leverage {spec.leverage}x exceeds desk cap {config['max_leverage']}x")
    if spec.position_size_pct > config["max_position_size_pct"]:
        errors.append(
            f"position_size_pct {spec.position_size_pct:.0%} exceeds desk cap "
            f"{config['max_position_size_pct']:.0%}"
        )
    notional_exposure = spec.position_size_pct * spec.leverage
    if notional_exposure > 2.0:  # matches risk/gate.py's MAX_NOTIONAL_EXPOSURE
        errors.append(
            f"notional exposure {notional_exposure:.2f} (size × leverage) exceeds max 2.00"
        )

    if spec.stop_loss_pct <= 0:
        errors.append("stop_loss_pct must be positive")
    if spec.take_profit_pct <= 0:
        errors.append("take_profit_pct must be positive")

    return errors
```

- [ ] **Step 6: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_dsl.py tests/test_validator.py -v`
Expected: 8 passed.

- [ ] **Step 7: Add PyYAML to requirements and commit**

Add `pyyaml==6.0.2` to `requirements.txt` if a version pin isn't already present for it (check first — `config.yaml` loading elsewhere in this codebase likely already depends on PyYAML; if `requirements.txt` already lists it, skip this edit).

```bash
git add backtest/ tests/test_dsl.py tests/test_validator.py requirements.txt
git commit -m "feat(backtest): add strategy-spec DSL schema, YAML loader, and validator"
```

**Definition of done:** `load_spec` parses a spec YAML file into a typed `Spec`; `validate_spec` rejects an unknown feature reference, a threshold list missing its `else` catch-all, an inverted scale/confidence threshold pair, and any leverage/position-size combination that would fail `risk/gate.py`'s live checks.

---

### Task 4: Interpreter

**Files:**
- Create: `backtest/interpreter.py`
- Test: `tests/test_interpreter.py`

**Interfaces:**
- Consumes: `backtest.dsl.Spec`, `EvidenceTerm`, `Threshold` (Task 3).
- Produces: `evaluate(spec: Spec, feature_row: dict) -> dict` — returns `{"action": "enter"|"wait", "asset": str|None, "direction": str|None, "confidence": float, "evidence_strength": dict, "reason": str}`, the same shape `agents/decision_loop.py` already handles from an LLM call (`response.get("confidence")`, `response.get("evidence_strength")`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_interpreter.py`:

```python
from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.interpreter import evaluate


def _spec(evidence, confidence_threshold=0.70, scale_threshold=0.50, direction="short"):
    return Spec(
        agent_id="test", spec_version=1, thesis_version=1,
        universe_include=["FET-PERP"], regime_exclude=[],
        direction=direction, confidence_threshold=confidence_threshold,
        scale_threshold=scale_threshold, evidence=evidence, secondary_evidence=[],
        stop_loss_pct=0.03, take_profit_pct=0.06, max_hold_hours=24,
        leverage=3, position_size_pct=0.10,
    )


def test_high_confidence_enters():
    spec = _spec([
        EvidenceTerm(
            name="unlock_size", feature="unlock_size_pct_float",
            thresholds=[Threshold(op=">=", value=0.03, weight=0.8), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ])
    result = evaluate(spec, {"unlock_size_pct_float": 0.05})
    assert result["action"] == "enter"
    assert result["direction"] == "short"
    assert result["confidence"] == 0.8
    assert result["evidence_strength"] == {"unlock_size": 0.8}


def test_low_confidence_waits():
    spec = _spec([
        EvidenceTerm(
            name="unlock_size", feature="unlock_size_pct_float",
            thresholds=[Threshold(op=">=", value=0.03, weight=0.8), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ])
    result = evaluate(spec, {"unlock_size_pct_float": 0.001})  # hits the else -> weight 0.0
    assert result["action"] == "wait"
    assert result["confidence"] == 0.0


def test_missing_feature_veto_forces_wait():
    spec = _spec([
        EvidenceTerm(
            name="unlock_size", feature="unlock_size_pct_float",
            thresholds=[Threshold(op=">=", value=0.03, weight=0.8), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ])
    result = evaluate(spec, {})  # feature entirely absent from the row
    assert result["action"] == "wait"
    assert "unlock_size" in result["reason"]


def test_missing_feature_skip_contributes_zero_no_veto():
    spec = _spec([
        EvidenceTerm(
            name="a", feature="funding_zscore",
            thresholds=[Threshold(op=">", value=2.0, weight=0.6), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
        EvidenceTerm(
            name="b", feature="missing_optional_feature",
            thresholds=[Threshold(op=">", value=0.0, weight=0.3), Threshold(op="else", weight=0.0)],
            missing="skip",
        ),
    ], confidence_threshold=0.5, scale_threshold=0.3)
    result = evaluate(spec, {"funding_zscore": 3.0})  # only 'a' present; 'b' skips, no veto
    assert result["action"] == "enter"
    assert result["confidence"] == 0.6
    assert "b" not in result["evidence_strength"]


def test_between_operator():
    spec = _spec([
        EvidenceTerm(
            name="days_to_event", feature="days_to_next_unlock",
            thresholds=[
                Threshold(op="between", value=[1, 4], weight=0.6),
                Threshold(op="between", value=[4, 10], weight=0.3),
                Threshold(op="else", weight=0.0),
            ],
            missing="veto",
        ),
    ], confidence_threshold=0.5, scale_threshold=0.3)
    result = evaluate(spec, {"days_to_next_unlock": 2})
    assert result["confidence"] == 0.6


def test_scaled_size_between_scale_and_confidence_threshold():
    spec = _spec([
        EvidenceTerm(
            name="a", feature="funding_zscore",
            thresholds=[Threshold(op=">", value=1.0, weight=0.55), Threshold(op="else", weight=0.0)],
            missing="veto",
        ),
    ], confidence_threshold=0.70, scale_threshold=0.50)
    result = evaluate(spec, {"funding_zscore": 1.5})
    assert result["action"] == "enter"
    assert result["confidence"] == 0.55
    assert result["reason"]  # non-empty, notes this is a scaled (not full) entry
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_interpreter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.interpreter'`.

- [ ] **Step 3: Implement**

Create `backtest/interpreter.py`:

```python
"""backtest/interpreter.py -- evaluates a Spec against one feature row.

Deterministic, pure, no I/O. Produces the same {action, confidence,
evidence_strength} shape an LLM decision already produces (see
agents/decision_loop.py's run_decision -- response.get("confidence"),
response.get("evidence_strength")), so a compiled agent can eventually call
this instead of an LLM (M8, out of scope here) with zero downstream changes.
"""
from __future__ import annotations

from backtest.dsl import EvidenceTerm, Spec, Threshold

_OPS = {
    ">": lambda v, x: v > x,
    ">=": lambda v, x: v >= x,
    "<": lambda v, x: v < x,
    "<=": lambda v, x: v <= x,
    "==": lambda v, x: v == x,
    "between": lambda v, x: x[0] <= v <= x[1],
}


def _threshold_weight(thresholds: list[Threshold], value: float) -> float:
    for t in thresholds:
        if t.op == "else":
            return t.weight
        if _OPS[t.op](value, t.value):
            return t.weight
    return 0.0  # unreachable if validate_spec enforced an 'else' catch-all


class _Veto(Exception):
    def __init__(self, term_name: str):
        self.term_name = term_name


def _score_term(term: EvidenceTerm, feature_row: dict, evidence_strength: dict) -> float:
    if term.feature not in feature_row or feature_row[term.feature] is None:
        if term.missing == "veto":
            raise _Veto(term.name)
        if term.missing == "skip":
            return 0.0
        if term.missing.startswith("uncertainty:"):
            penalty = float(term.missing.split(":", 1)[1])
            return penalty
        return 0.0

    weight = _threshold_weight(term.thresholds, feature_row[term.feature])
    evidence_strength[term.name] = weight
    return weight


def evaluate(spec: Spec, feature_row: dict) -> dict:
    """Evaluate `spec` against `feature_row` (a flat dict of feature name ->
    value, as produced by market/heartbeat.py's compute_replayable_fields
    for one asset). Returns a wait or enter decision."""
    evidence_strength: dict = {}
    try:
        total = sum(_score_term(t, feature_row, evidence_strength) for t in spec.evidence)
        total += sum(_score_term(t, feature_row, evidence_strength) for t in spec.secondary_evidence)
    except _Veto as veto:
        return {
            "action": "wait",
            "asset": None,
            "direction": None,
            "confidence": 0.0,
            "evidence_strength": evidence_strength,
            "reason": f"required evidence '{veto.term_name}' missing from feature row (veto)",
        }

    confidence = max(0.0, min(1.0, total))

    if confidence < spec.scale_threshold:
        return {
            "action": "wait",
            "asset": None,
            "direction": None,
            "confidence": confidence,
            "evidence_strength": evidence_strength,
            "reason": f"confidence {confidence:.2f} below scale_threshold {spec.scale_threshold}",
        }

    scaled = confidence < spec.confidence_threshold
    return {
        "action": "enter",
        "asset": None,  # filled in by the caller, which knows which asset this row is for
        "direction": spec.direction,
        "confidence": confidence,
        "evidence_strength": evidence_strength,
        "reason": (
            f"scaled entry: confidence {confidence:.2f} between scale_threshold "
            f"{spec.scale_threshold} and confidence_threshold {spec.confidence_threshold}"
            if scaled else
            f"full-size entry: confidence {confidence:.2f} >= confidence_threshold {spec.confidence_threshold}"
        ),
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_interpreter.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add backtest/interpreter.py tests/test_interpreter.py
git commit -m "feat(backtest): add the spec interpreter"
```

**Definition of done:** `evaluate(spec, feature_row)` deterministically reproduces the confidence-scoring and missing-data-degradation semantics every thesis's prose already describes, in the same decision shape an LLM call produces.

---

### Task 5: Historical backfill

**Files:**
- Modify: `market/hyperliquid.py`
- Create: `scripts/backfill_history.py`
- Test: `tests/test_backfill_history.py`

**Interfaces:**
- Modifies: `HyperliquidClient.get_ohlcv` gains optional `start_ms`/`end_ms` parameters (default `None`, preserving today's "relative to now" behavior when omitted).
- Produces: `backfill(universe: list[str], provider, months: int = 12, days_5m: int = 90) -> dict` in `scripts/backfill_history.py` — returns a summary dict; writes directly to the ledger via `store.ledger.append_ledger_record`.

- [ ] **Step 1: Extend `get_ohlcv` for an explicit historical range**

In `market/hyperliquid.py`, locate `get_ohlcv` (around line 151-166):

```python
    async def get_ohlcv(
        self, asset: str, interval: str, lookback_candles: int
    ) -> list[list]:
        now_ms = int(time.time() * 1000)
        interval_ms = _interval_to_ms(interval)
        start_ms = now_ms - lookback_candles * interval_ms
        data = await self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": self._normalize_asset(asset),
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": now_ms,
                },
            }
```

Replace with:

```python
    async def get_ohlcv(
        self, asset: str, interval: str, lookback_candles: int,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> list[list]:
        """Fetch OHLCV candles. Default behavior (start_ms/end_ms omitted)
        is unchanged: `lookback_candles` back from now. Pass both to fetch
        an explicit historical range instead -- used by
        scripts/backfill_history.py, which needs candles from a year ago,
        not "N candles back from the current moment"."""
        now_ms = int(time.time() * 1000)
        interval_ms = _interval_to_ms(interval)
        if start_ms is None:
            start_ms = now_ms - lookback_candles * interval_ms
        if end_ms is None:
            end_ms = now_ms
        data = await self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": self._normalize_asset(asset),
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
```

(Leave the rest of the function body unchanged -- only the signature and the `start_ms`/`end_ms` resolution at the top change.)

- [ ] **Step 2: Write the failing test for the extension**

Add to a new or existing hyperliquid test file (`tests/test_hyperliquid.py` already exists per this project's CLAUDE.md notes -- append to it):

```python
@pytest.mark.asyncio
@respx.mock
async def test_get_ohlcv_explicit_range_overrides_default(hyperliquid_client_config):
    """When start_ms/end_ms are passed, they're used verbatim instead of
    being derived from lookback_candles relative to now."""
    from market.hyperliquid import HyperliquidClient

    captured = {}

    def _capture(request):
        import json
        body = json.loads(request.content)
        captured["startTime"] = body["req"]["startTime"]
        captured["endTime"] = body["req"]["endTime"]
        return httpx.Response(200, json=[])

    respx.post("https://api.hyperliquid.xyz/info").mock(side_effect=_capture)

    client = HyperliquidClient(hyperliquid_client_config)
    await client.get_ohlcv("BTC-PERP", "1h", lookback_candles=100, start_ms=1000, end_ms=2000)

    assert captured["startTime"] == 1000
    assert captured["endTime"] == 2000
```

Check `tests/test_hyperliquid.py`'s existing fixtures/imports first (`httpx`, `respx`, whatever constructs a `HyperliquidClient` in the existing tests) and match this new test's setup to those exact conventions rather than inventing a different pattern.

- [ ] **Step 3: Run to verify failure, then pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_hyperliquid.py -v`
Expected: the new test fails before Step 1's edit lands (if written first) and passes after. If Step 1 is already applied by the time this runs, confirm RED by temporarily reverting the signature change, then re-apply and confirm GREEN -- do not skip proving the test actually exercises the new parameters.

- [ ] **Step 4: Implement the backfill script**

Create `scripts/backfill_history.py`:

```python
#!/usr/bin/env python
"""scripts/backfill_history.py -- one-time historical backfill into the ledger.

Backfills 1h candles + funding (12 months) and 5m candles (90 days) for the
full universe directly into ledger/{kind}/{YYYY-MM}.{jsonl,parquet} via
store.ledger.append_ledger_record, dated by each candle's own historical
timestamp (not "now"), so backfilled rows compact and decay through the
exact same monthly pipeline as organically-captured data.

OI and liquidations are NOT backfilled -- Hyperliquid has no OI history
endpoint and Coinalyze's free tier doesn't backfill either; both remain
live-accumulated only, per docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import yaml

from market.hyperliquid import HyperliquidClient
from store.ledger import append_ledger_record

DEFAULT_CANDLE_MONTHS = 12
DEFAULT_5M_DAYS = 90


async def _backfill_asset_1h_and_funding(client: HyperliquidClient, asset: str, months: int) -> dict:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - months * 30 * 24 * 3600 * 1000

    candles = await client.get_ohlcv(asset, "1h", lookback_candles=0, start_ms=start_ms, end_ms=end_ms)
    for c in candles:
        ts, o, h, l, close, v = c
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        append_ledger_record(
            "candles_1h", {"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset,
                           "o": o, "h": h, "l": l, "c": close, "v": v},
            when,
        )

    funding = await client.get_funding_history(asset, start_ms)
    for f in funding:
        rate = f.get("fundingRate")
        ts = f.get("time")
        if rate is None or ts is None:
            continue
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        append_ledger_record(
            "funding", {"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset, "rate": rate},
            when,
        )

    return {"asset": asset, "candles_1h": len(candles), "funding": len(funding)}


async def _backfill_asset_5m(client: HyperliquidClient, asset: str, days: int) -> dict:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000

    candles = await client.get_ohlcv(asset, "5m", lookback_candles=0, start_ms=start_ms, end_ms=end_ms)
    for c in candles:
        ts, o, h, l, close, v = c
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        append_ledger_record(
            "candles_5m", {"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset,
                           "o": o, "h": h, "l": l, "c": close, "v": v},
            when,
        )
    return {"asset": asset, "candles_5m": len(candles)}


async def backfill(
    universe: list[str], provider: HyperliquidClient,
    months: int = DEFAULT_CANDLE_MONTHS, days_5m: int = DEFAULT_5M_DAYS,
) -> dict:
    """Backfill 1h candles + funding (`months` back) and 5m candles
    (`days_5m` back) for every asset in `universe`. Returns a per-asset
    summary dict. Best-effort per asset -- one asset's failure is logged
    and does not stop the rest (matches append_ledger_record's own
    never-block contract for the writes themselves; the network fetch here
    is the one part of this script that CAN legitimately fail per-asset)."""
    results = {}
    for asset in universe:
        try:
            hourly = await _backfill_asset_1h_and_funding(provider, asset, months)
            five_min = await _backfill_asset_5m(provider, asset, days_5m)
            results[asset] = {**hourly, **five_min}
            print(f"{asset}: {results[asset]}")
        except Exception as exc:
            results[asset] = {"error": str(exc)}
            print(f"{asset}: FAILED - {exc}")
    return results


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical candles+funding into the ledger")
    parser.add_argument("--months", type=int, default=DEFAULT_CANDLE_MONTHS)
    parser.add_argument("--days-5m", type=int, default=DEFAULT_5M_DAYS)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    universe = config["universe"]

    # HyperliquidClient() takes no arguments and is an async context manager
    # (market/hyperliquid.py:46-66; market/provider.py:16 constructs it the
    # same bare way) -- not a config-taking constructor with a close() method.
    async with HyperliquidClient() as client:
        await backfill(universe, client, args.months, args.days_5m)


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 5: Write the failing test for the backfill function**

Create `tests/test_backfill_history.py`:

```python
import json
from datetime import datetime, timezone

import pytest

from scripts.backfill_history import backfill


class _StubClient:
    def __init__(self, candles_1h, funding, candles_5m):
        self._candles_1h = candles_1h
        self._funding = funding
        self._candles_5m = candles_5m

    async def get_ohlcv(self, asset, interval, lookback_candles=0, start_ms=None, end_ms=None):
        return self._candles_1h if interval == "1h" else self._candles_5m

    async def get_funding_history(self, asset, start_time_ms):
        return self._funding


@pytest.mark.asyncio
async def test_backfill_writes_candles_and_funding_to_ledger(tmp_path, monkeypatch):
    import store.ledger as ledger_module
    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))

    ts_ms = int(datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    stub = _StubClient(
        candles_1h=[[ts_ms, 100.0, 101.0, 99.0, 100.5, 10.0]],
        funding=[{"time": ts_ms, "fundingRate": 0.0001}],
        candles_5m=[[ts_ms, 100.0, 100.2, 99.9, 100.1, 1.0]],
    )

    summary = await backfill(["BTC-PERP"], stub, months=1, days_5m=1)

    assert summary["BTC-PERP"]["candles_1h"] == 1
    assert summary["BTC-PERP"]["funding"] == 1
    assert summary["BTC-PERP"]["candles_5m"] == 1

    candles_1h_file = tmp_path / "ledger" / "candles_1h" / "2025-06.jsonl"
    assert candles_1h_file.exists()
    record = json.loads(candles_1h_file.read_text(encoding="utf-8").strip())
    assert record["asset"] == "BTC-PERP"
    assert record["c"] == 100.5


@pytest.mark.asyncio
async def test_backfill_continues_after_one_asset_fails(tmp_path, monkeypatch):
    import store.ledger as ledger_module
    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))

    class _FailingClient:
        async def get_ohlcv(self, *a, **kw):
            raise RuntimeError("network error")

        async def get_funding_history(self, *a, **kw):
            raise RuntimeError("network error")

    summary = await backfill(["BAD-PERP"], _FailingClient(), months=1, days_5m=1)
    assert "error" in summary["BAD-PERP"]
```

- [ ] **Step 6: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_backfill_history.py tests/test_hyperliquid.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add market/hyperliquid.py scripts/backfill_history.py tests/test_backfill_history.py tests/test_hyperliquid.py
git commit -m "feat(backtest): add historical backfill (12mo 1h candles+funding, 90d 5m candles)"
```

**Definition of done:** `python scripts/backfill_history.py` populates the ledger's `candles_1h`, `funding`, and `candles_5m` streams with real Hyperliquid history for the full universe, in the exact partitioned format the rest of the ledger already uses; OI and liquidations are correctly left untouched (live-accumulated only).

---

### Task 6: Backtest engine

**Files:**
- Create: `backtest/engine.py`
- Test: `tests/test_backtest_engine.py`

**Interfaces:**
- Consumes: `backtest.dsl.Spec`, `backtest.interpreter.evaluate` (Tasks 3-4); `market.heartbeat.compute_replayable_fields` (Task 2); ledger data via a `_read_partitions`-style reader (matching the pattern already established in `scripts/rebuild_local_cache.py`, not reinvented).
- Produces: `run_backtest(spec: Spec, ledger_dir: Path, start: datetime, end: datetime, taker_fee: float) -> BacktestResult` where `BacktestResult` is a dataclass with `trades: list[dict]`, `equity_curve: list[tuple[datetime, float]]`, `total_return_pct: float`, `sharpe: float`, `data_window: dict` (per-stream actual coverage, so a spec leaning on thin OI/liquidation history reports that honestly rather than implying full-window validation).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backtest_engine.py`:

```python
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.engine import run_backtest


def _write_candles(ledger_dir: Path, kind: str, month: str, rows: list[dict]) -> None:
    path = ledger_dir / kind / f"{month}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _synthetic_candles(asset: str, start: datetime, n: int, base_price: float, drift: float) -> list[dict]:
    rows = []
    price = base_price
    for i in range(n):
        ts = start + timedelta(hours=i)
        price = price * (1 + drift)
        rows.append({
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset,
            "o": price, "h": price * 1.001, "l": price * 0.999, "c": price, "v": 100.0,
        })
    return rows


def test_run_backtest_produces_equity_curve_and_trades(tmp_path):
    ledger_dir = tmp_path / "ledger"
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = _synthetic_candles("FET-PERP", start, 400, base_price=1.0, drift=0.001)
    _write_candles(ledger_dir, "candles_1h", "2025-01", candles[:350])
    _write_candles(ledger_dir, "candles_1h", "2025-02", candles[350:])

    funding_rows = [
        {"ts": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": "FET-PERP", "rate": 0.0003}
        for i in range(400)
    ]
    _write_candles(ledger_dir, "funding", "2025-01", funding_rows[:350])
    _write_candles(ledger_dir, "funding", "2025-02", funding_rows[350:])

    spec = Spec(
        agent_id="test_spec", spec_version=1, thesis_version=1,
        universe_include=["FET-PERP"], regime_exclude=[],
        direction="long", confidence_threshold=0.5, scale_threshold=0.3,
        evidence=[EvidenceTerm(
            name="funding_positive", feature="funding_zscore",
            thresholds=[Threshold(op=">", value=-100.0, weight=0.6), Threshold(op="else", weight=0.0)],
            missing="veto",
        )],
        secondary_evidence=[],
        stop_loss_pct=0.05, take_profit_pct=0.10, max_hold_hours=48,
        leverage=2, position_size_pct=0.10,
    )

    result = run_backtest(spec, ledger_dir, start, start + timedelta(hours=399), taker_fee=0.00035)

    assert len(result.equity_curve) > 0
    assert result.data_window["candles_1h"]["rows"] == 400
    assert isinstance(result.total_return_pct, float)
    assert isinstance(result.sharpe, float)


def test_run_backtest_reports_thin_data_window_honestly(tmp_path):
    ledger_dir = tmp_path / "ledger"
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # Only 5 rows of OI -- far short of a full window; must be reported, not hidden.
    oi_rows = [
        {"ts": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": "FET-PERP", "oi": 1_000_000.0}
        for i in range(5)
    ]
    _write_candles(ledger_dir, "oi", "2025-01", oi_rows)
    candles = _synthetic_candles("FET-PERP", start, 10, base_price=1.0, drift=0.0)
    _write_candles(ledger_dir, "candles_1h", "2025-01", candles)

    spec = Spec(
        agent_id="test_spec", spec_version=1, thesis_version=1,
        universe_include=["FET-PERP"], regime_exclude=[],
        direction="long", confidence_threshold=0.9, scale_threshold=0.9,
        evidence=[EvidenceTerm(
            name="oi_check", feature="oi_zscore",
            thresholds=[Threshold(op="else", weight=0.0)], missing="skip",
        )],
        secondary_evidence=[],
        stop_loss_pct=0.05, take_profit_pct=0.10, max_hold_hours=48,
        leverage=2, position_size_pct=0.10,
    )

    result = run_backtest(spec, ledger_dir, start, start + timedelta(hours=9), taker_fee=0.00035)

    assert result.data_window["oi"]["rows"] == 5
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_backtest_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.engine'`.

- [ ] **Step 3: Implement**

Create `backtest/engine.py`:

```python
"""backtest/engine.py -- replay historical ledger data through the
interpreter, using the exact same feature-computation core the live
heartbeat uses (market.heartbeat.compute_replayable_fields).

Fee model matches the paper bridge's taker_fee. Slippage is a fixed,
conservative assumption (not execute_close's live slippage_estimate,
which needs order-book depth the ledger never captures) -- see
docs/superpowers/specs/2026-07-07-strategy-spec-dsl-backtester-design.md
section 3 for why this gap is real and stays documented, not hidden.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest.dsl import Spec
from backtest.interpreter import evaluate
from market.heartbeat import compute_replayable_fields

# Fixed backtest slippage assumption (pct of price), applied against the
# entry direction. Conservative relative to typical observed spread+impact
# on this universe's liquid assets; revisit once live paper-vs-backtest
# divergence data exists to calibrate against.
BACKTEST_SLIPPAGE_PCT = 0.0005

MIN_CANDLES_FOR_FEATURES = 20  # compute_replayable_fields needs enough history for ATR/RSI/EMA


@dataclass
class BacktestResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    total_return_pct: float = 0.0
    sharpe: float = 0.0
    data_window: dict = field(default_factory=dict)


def _read_partitions(ledger_dir: Path, kind: str, asset: str) -> pd.DataFrame:
    kind_dir = ledger_dir / kind
    if not kind_dir.exists():
        return pd.DataFrame()
    frames = [pd.read_parquet(p) for p in sorted(kind_dir.glob("*.parquet"))]
    frames += [pd.read_json(p, lines=True) for p in sorted(kind_dir.glob("*.jsonl"))]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "asset" in df.columns:
        df = df[df["asset"] == asset]
    if df.empty:
        return df
    df["_ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("_ts").reset_index(drop=True)


def _to_candle_list(df: pd.DataFrame) -> list[list]:
    return [
        [int(row["_ts"].timestamp() * 1000), row["o"], row["h"], row["l"], row["c"], row["v"]]
        for _, row in df.iterrows()
    ]


def _to_funding_history(df: pd.DataFrame) -> list[dict]:
    return [{"time": int(row["_ts"].timestamp() * 1000), "fundingRate": row["rate"]} for _, row in df.iterrows()]


def run_backtest(
    spec: Spec, ledger_dir: Path, start: datetime, end: datetime, taker_fee: float,
) -> BacktestResult:
    result = BacktestResult()
    balance = 10_000.0  # notional backtest starting balance; only relative return matters
    peak = balance
    open_position: dict | None = None
    returns_per_bar: list[float] = []

    for asset in spec.universe_include:
        candles_df = _read_partitions(ledger_dir, "candles_1h", asset)
        funding_df = _read_partitions(ledger_dir, "funding", asset)
        oi_df = _read_partitions(ledger_dir, "oi", asset)

        result.data_window.setdefault("candles_1h", {"rows": 0})
        result.data_window.setdefault("funding", {"rows": 0})
        result.data_window.setdefault("oi", {"rows": 0})
        result.data_window["candles_1h"]["rows"] += len(candles_df)
        result.data_window["funding"]["rows"] += len(funding_df)
        result.data_window["oi"]["rows"] += len(oi_df)

        if candles_df.empty:
            continue

        in_window = candles_df[(candles_df["_ts"] >= pd.Timestamp(start, tz="UTC")) &
                                (candles_df["_ts"] <= pd.Timestamp(end, tz="UTC"))]

        oi_values = oi_df["oi"].tolist() if not oi_df.empty else []

        for idx in range(len(in_window)):
            bar_ts = in_window.iloc[idx]["_ts"]
            history_df = candles_df[candles_df["_ts"] <= bar_ts].tail(300)
            if len(history_df) < MIN_CANDLES_FOR_FEATURES:
                continue

            candles = _to_candle_list(history_df)
            funding_window = funding_df[funding_df["_ts"] <= bar_ts]
            funding_history = _to_funding_history(funding_window)
            funding_val = funding_history[-1]["fundingRate"] if funding_history else None

            oi_window = oi_df[oi_df["_ts"] <= bar_ts]["oi"].tolist() if not oi_df.empty else []
            oi_val = oi_window[-1] if oi_window else None
            prior_oi_history = oi_window[:-1] if len(oi_window) > 1 else []

            feature_row = compute_replayable_fields(
                candles, funding_history, oi_val, funding_val, prior_oi_history,
            )
            price = feature_row["price"]

            if open_position is not None and open_position["asset"] == asset:
                entry = open_position["entry_price"]
                direction = open_position["direction"]
                pct_move = (price - entry) / entry if direction == "long" else (entry - price) / entry
                hit_sl = pct_move <= -spec.stop_loss_pct
                hit_tp = pct_move >= spec.take_profit_pct
                held_hours = (bar_ts - open_position["opened_at"]).total_seconds() / 3600
                timed_out = held_hours >= spec.max_hold_hours
                if hit_sl or hit_tp or timed_out:
                    exit_price = price * (1 - BACKTEST_SLIPPAGE_PCT if direction == "long" else 1 + BACKTEST_SLIPPAGE_PCT)
                    gross_pct = pct_move * spec.leverage
                    net_pct = gross_pct - 2 * taker_fee * spec.leverage
                    pnl_usd = balance * spec.position_size_pct * net_pct
                    balance += pnl_usd
                    peak = max(peak, balance)
                    returns_per_bar.append(net_pct)
                    result.trades.append({
                        "asset": asset, "direction": direction,
                        "entry_price": entry, "exit_price": exit_price,
                        "opened_at": open_position["opened_at"], "closed_at": bar_ts,
                        "pnl_pct": net_pct, "pnl_usd": pnl_usd,
                        "reason": "stop_loss" if hit_sl else ("take_profit" if hit_tp else "max_hold"),
                    })
                    result.equity_curve.append((bar_ts.to_pydatetime(), balance))
                    open_position = None
                continue

            if open_position is None:
                decision = evaluate(spec, feature_row)
                if decision["action"] == "enter":
                    open_position = {
                        "asset": asset, "direction": decision["direction"],
                        "entry_price": price, "opened_at": bar_ts,
                    }

    result.total_return_pct = (balance - 10_000.0) / 10_000.0
    if len(returns_per_bar) >= 2:
        mean_r = statistics.mean(returns_per_bar)
        std_r = statistics.stdev(returns_per_bar)
        result.sharpe = (mean_r / std_r) * (len(returns_per_bar) ** 0.5) if std_r > 0 else 0.0
    return result
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_backtest_engine.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backtest/engine.py tests/test_backtest_engine.py
git commit -m "feat(backtest): add the backtest replay engine"
```

**Definition of done:** `run_backtest` replays a spec against ledger history using the identical feature-computation core the live heartbeat uses, produces trades + an equity curve + a Sharpe estimate, and reports the actual per-stream data window used rather than implying uniform coverage across features with very different real history depth.

---

### Task 7: Walk-forward harness + overfit metrics

**Files:**
- Create: `backtest/walk_forward.py`
- Test: `tests/test_walk_forward.py`

**Interfaces:**
- Consumes: `backtest.engine.run_backtest`, `backtest.dsl.Spec`.
- Produces: `run_walk_forward(spec: Spec, ledger_dir: Path, taker_fee: float) -> WalkForwardReport` — a dataclass with `train: BacktestResult`, `validate: BacktestResult`, `test: BacktestResult`, `deflated_sharpe: float`, `parameter_sensitivity: dict[str, float]` (each perturbed parameter name -> resulting test-window Sharpe delta).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_walk_forward.py`:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.walk_forward import run_walk_forward
from tests.test_backtest_engine import _synthetic_candles, _write_candles


def _spec():
    return Spec(
        agent_id="test_spec", spec_version=1, thesis_version=1,
        universe_include=["FET-PERP"], regime_exclude=[],
        direction="long", confidence_threshold=0.5, scale_threshold=0.3,
        evidence=[EvidenceTerm(
            name="funding_positive", feature="funding_zscore",
            thresholds=[Threshold(op=">", value=-100.0, weight=0.6), Threshold(op="else", weight=0.0)],
            missing="veto",
        )],
        secondary_evidence=[],
        stop_loss_pct=0.05, take_profit_pct=0.10, max_hold_hours=48,
        leverage=2, position_size_pct=0.10,
    )


def _seed_ledger(tmp_path: Path) -> Path:
    ledger_dir = tmp_path / "ledger"
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = _synthetic_candles("FET-PERP", start, 1000, base_price=1.0, drift=0.0005)
    for month, chunk_start in (("2025-01", 0), ("2025-02", 350), ("2025-03", 700)):
        _write_candles(ledger_dir, "candles_1h", month, candles[chunk_start:chunk_start + 350])
    funding_rows = [
        {"ts": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": "FET-PERP", "rate": 0.0003}
        for i in range(1000)
    ]
    for month, chunk_start in (("2025-01", 0), ("2025-02", 350), ("2025-03", 700)):
        _write_candles(ledger_dir, "funding", month, funding_rows[chunk_start:chunk_start + 350])
    return ledger_dir


def test_walk_forward_splits_into_three_windows(tmp_path):
    ledger_dir = _seed_ledger(tmp_path)
    report = run_walk_forward(_spec(), ledger_dir, taker_fee=0.00035)

    assert report.train is not None
    assert report.validate is not None
    assert report.test is not None
    assert isinstance(report.deflated_sharpe, float)


def test_walk_forward_reports_parameter_sensitivity(tmp_path):
    ledger_dir = _seed_ledger(tmp_path)
    report = run_walk_forward(_spec(), ledger_dir, taker_fee=0.00035)

    assert "confidence_threshold" in report.parameter_sensitivity
    assert "stop_loss_pct" in report.parameter_sensitivity
    assert all(isinstance(v, float) for v in report.parameter_sensitivity.values())
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_walk_forward.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.walk_forward'`.

- [ ] **Step 3: Implement**

Create `backtest/walk_forward.py`:

```python
"""backtest/walk_forward.py -- train/validate/test split, deflated Sharpe,
and a parameter-sensitivity sweep.

Single 70/15/15 split, not rolling -- real history depth varies too much by
feature (12mo candles/funding vs. days of OI/liquidations) to justify a
rolling harness yet. See
docs/superpowers/specs/2026-07-07-strategy-spec-dsl-backtester-design.md
section 3.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest.dsl import Spec
from backtest.engine import BacktestResult, run_backtest

TRAIN_FRACTION = 0.70
VALIDATE_FRACTION = 0.15
# remaining 0.15 is the test window

PERTURBATION_PCT = 0.20


@dataclass
class WalkForwardReport:
    train: BacktestResult
    validate: BacktestResult
    test: BacktestResult
    deflated_sharpe: float = 0.0
    parameter_sensitivity: dict = field(default_factory=dict)


def _ledger_date_range(ledger_dir: Path, spec: Spec) -> tuple[datetime, datetime]:
    """Full available candles_1h date range across the spec's universe."""
    kind_dir = ledger_dir / "candles_1h"
    all_ts = []
    for path in sorted(kind_dir.glob("*.jsonl")) + sorted(kind_dir.glob("*.parquet")):
        df = pd.read_json(path, lines=True) if path.suffix == ".jsonl" else pd.read_parquet(path)
        if "asset" in df.columns:
            df = df[df["asset"].isin(spec.universe_include)]
        if not df.empty:
            all_ts.extend(pd.to_datetime(df["ts"], utc=True).tolist())
    if not all_ts:
        now = datetime.now(timezone.utc)
        return now, now
    return min(all_ts).to_pydatetime(), max(all_ts).to_pydatetime()


def _deflated_sharpe(sharpe: float, n_trials: int, n_returns: int) -> float:
    """Simplified deflated Sharpe: penalizes the raw Sharpe for the number
    of parameter combinations effectively searched (n_trials, here the
    parameter-sensitivity sweep's trial count) and the sample size backing
    it. A conservative approximation, not the full Bailey-Lopez-de-Prado
    formula -- adequate for flagging "this edge is likely noise" without
    requiring a probability-distribution library dependency."""
    if n_returns < 2:
        return 0.0
    import math

    trial_penalty = math.sqrt(2 * math.log(max(n_trials, 1))) / math.sqrt(n_returns)
    return sharpe - trial_penalty


def run_walk_forward(spec: Spec, ledger_dir: Path, taker_fee: float) -> WalkForwardReport:
    full_start, full_end = _ledger_date_range(ledger_dir, spec)
    total_seconds = (full_end - full_start).total_seconds()

    train_end = full_start + (full_end - full_start) * TRAIN_FRACTION
    validate_end = train_end + (full_end - full_start) * VALIDATE_FRACTION

    train_result = run_backtest(spec, ledger_dir, full_start, train_end, taker_fee)
    validate_result = run_backtest(spec, ledger_dir, train_end, validate_end, taker_fee)
    test_result = run_backtest(spec, ledger_dir, validate_end, full_end, taker_fee)

    sensitivity = {}
    perturbable = ("confidence_threshold", "scale_threshold", "stop_loss_pct", "take_profit_pct")
    for field_name in perturbable:
        base_value = getattr(spec, field_name)
        perturbed_spec = dataclasses.replace(spec, **{field_name: base_value * (1 + PERTURBATION_PCT)})
        perturbed_result = run_backtest(perturbed_spec, ledger_dir, validate_end, full_end, taker_fee)
        sensitivity[field_name] = perturbed_result.sharpe - test_result.sharpe

    deflated = _deflated_sharpe(test_result.sharpe, n_trials=len(perturbable) + 1, n_returns=len(test_result.trades))

    return WalkForwardReport(
        train=train_result, validate=validate_result, test=test_result,
        deflated_sharpe=deflated, parameter_sensitivity=sensitivity,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_walk_forward.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backtest/walk_forward.py tests/test_walk_forward.py
git commit -m "feat(backtest): add walk-forward harness with deflated Sharpe and parameter-sensitivity sweep"
```

**Definition of done:** `run_walk_forward` splits available history 70/15/15, backtests all three windows, and reports both a deflated Sharpe and how much the test-window Sharpe moves under a ±20% perturbation of each key threshold — the signal that separates a real edge from a curve-fit one.

---

### Task 8: Hand-compile 3 seed specs and run the first real backtests

**Files:**
- Create: `agents/specs/silver_basin_v1.yaml`, `agents/specs/iron_moth_v1.yaml`, `agents/specs/steel_crane_v1.yaml`
- Create: `scripts/run_seed_backtests.py`
- Test: `tests/test_seed_specs.py`

**Interfaces:**
- Consumes: everything from Tasks 3-7.
- Produces: three valid, `validate_spec`-passing YAML specs compiled directly from the existing thesis prose in `scripts/fresh_start.py`'s `SEED_AGENTS`; a runner script that backfills (if not already done), backtests all three through walk-forward, and prints a report.

- [ ] **Step 1: Write the failing spec-validity tests**

Create `tests/test_seed_specs.py`:

```python
import glob

import pytest
import yaml

from backtest.dsl import load_spec
from backtest.validator import validate_spec

CONFIG = {"max_leverage": 10, "max_position_size_pct": 0.20, "max_concurrent_positions": 3}


@pytest.mark.parametrize("spec_path", sorted(glob.glob("agents/specs/*.yaml")))
def test_seed_spec_is_valid(spec_path):
    spec = load_spec(spec_path)
    errors = validate_spec(spec, CONFIG)
    assert errors == [], f"{spec_path}: {errors}"


def test_three_seed_specs_exist():
    paths = sorted(glob.glob("agents/specs/*.yaml"))
    agent_ids = {load_spec(p).agent_id for p in paths}
    assert agent_ids == {"silver_basin", "iron_moth", "steel_crane"}
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_seed_specs.py -v`
Expected: FAIL — no files match `agents/specs/*.yaml`.

- [ ] **Step 3: Hand-compile the three specs**

Create `agents/specs/silver_basin_v1.yaml`, compiling directly from `silver_basin`'s thesis text in `scripts/fresh_start.py` (funding rate z-score primary evidence, funding acceleration, OI-funding alignment; direction determined by z-score sign — split into two direction-specific specs is out of scope for this hand-compile, so encode the more common/first-listed case: positive extreme -> short):

```yaml
agent_id: silver_basin
spec_version: 1
thesis_version: 1

universe:
  include: [SOL-PERP, ETH-PERP, ARB-PERP, OP-PERP, SUI-PERP]

regime_filter:
  exclude: []

entry:
  direction: short
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: funding_extremity
      feature: funding_zscore
      thresholds:
        - {op: ">", value: 2.0, weight: 0.7}
        - {op: ">", value: 1.5, weight: 0.5}
        - {op: ">", value: 1.0, weight: 0.2}
        - {op: "else", weight: 0.0}
      missing: veto
    - name: funding_acceleration
      feature: funding_acceleration
      thresholds:
        - {op: ">", value: 0.0, weight: 0.5}
        - {op: "else", weight: -0.4}
      missing: skip
  secondary_evidence:
    - name: oi_funding_alignment
      feature: oi_drawdown_pct
      thresholds:
        - {op: "<", value: -0.01, weight: 0.3}
        - {op: "else", weight: 0.1}
      missing: uncertainty:-0.1

exit:
  stop_loss_pct: 0.02
  take_profit_pct: 0.04
  max_hold_hours: 8

position:
  leverage: 4
  position_size_pct: 0.10
```

Create `agents/specs/iron_moth_v1.yaml`, compiling from `iron_moth`'s cross-sectional momentum thesis (momentum acceleration, volatility-adjusted return proxy via ATR percentile):

```yaml
agent_id: iron_moth
spec_version: 1
thesis_version: 1

universe:
  include: [SOL-PERP, ETH-PERP, SUI-PERP, AVAX-PERP, LINK-PERP]

regime_filter:
  exclude: [range_high_vol]

entry:
  direction: long
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: momentum_acceleration
      feature: momentum_acceleration
      thresholds:
        - {op: ">", value: 0.001, weight: 0.5}
        - {op: ">", value: 0.0002, weight: 0.2}
        - {op: "else", weight: -0.4}
      missing: veto
    - name: volatility_adjusted
      feature: atr_percentile
      thresholds:
        - {op: "between", value: [0.3, 0.7], weight: 0.6}
        - {op: "else", weight: 0.1}
      missing: skip
  secondary_evidence:
    - name: volume_confirmation
      feature: volume_percentile_14d
      thresholds:
        - {op: ">", value: 0.5, weight: 0.2}
        - {op: "else", weight: -0.2}
      missing: skip

exit:
  stop_loss_pct: 0.025
  take_profit_pct: 0.05
  max_hold_hours: 12

position:
  leverage: 3
  position_size_pct: 0.12
```

Create `agents/specs/steel_crane_v1.yaml`, compiling from `steel_crane`'s liquidation-hunter thesis (real Coinalyze `liq_total_usd`/direction fields replace the old cascade proxy, per FORGE_PROPOSAL M7b's "replacing the proxy" goal):

```yaml
agent_id: steel_crane
spec_version: 1
thesis_version: 1

universe:
  include: [SOL-PERP, SUI-PERP, ARB-PERP, OP-PERP, ETH-PERP]

regime_filter:
  exclude: [crisis]

entry:
  direction: long
  confidence_threshold: 0.70
  scale_threshold: 0.50
  evidence:
    - name: liquidation_volume
      feature: liq_total_usd
      thresholds:
        - {op: ">", value: 10000000, weight: 0.8}
        - {op: ">", value: 5000000, weight: 0.6}
        - {op: ">", value: 2000000, weight: 0.3}
        - {op: "else", weight: 0.0}
      missing: veto
    - name: oi_drawdown_during_cascade
      feature: oi_drawdown_pct
      thresholds:
        - {op: "<", value: -0.05, weight: 0.6}
        - {op: "<", value: -0.03, weight: 0.4}
        - {op: "else", weight: -0.2}
      missing: skip
  secondary_evidence:
    - name: pre_cascade_funding_extremity
      feature: funding_zscore
      thresholds:
        - {op: "between", value: [1.5, 100], weight: 0.3}
        - {op: "between", value: [-100, -1.5], weight: 0.3}
        - {op: "else", weight: 0.0}
      missing: skip

exit:
  stop_loss_pct: 0.02
  take_profit_pct: 0.04
  max_hold_hours: 4

position:
  leverage: 4
  position_size_pct: 0.08
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_seed_specs.py -v`
Expected: 4 passed (3 parametrized validity checks + the existence check).

- [ ] **Step 5: Write the seed-backtest runner**

Create `scripts/run_seed_backtests.py`:

```python
#!/usr/bin/env python
"""scripts/run_seed_backtests.py -- backtest the 3 hand-compiled seed specs.

Prints a report per spec: train/validate/test Sharpe, deflated Sharpe,
parameter sensitivity, and the actual data window used per feature stream
(honest about OI/liquidation-dependent specs having far less real history
than funding/price-driven ones). Does NOT run the backfill itself --
run scripts/backfill_history.py first.
"""
from __future__ import annotations

import glob
from pathlib import Path

import yaml

from backtest.dsl import load_spec
from backtest.walk_forward import run_walk_forward

LEDGER_DIR = Path("ledger")


def main() -> None:
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    taker_fee = config["desk"]["taker_fee"]

    for spec_path in sorted(glob.glob("agents/specs/*.yaml")):
        spec = load_spec(spec_path)
        report = run_walk_forward(spec, LEDGER_DIR, taker_fee)

        print(f"\n=== {spec.agent_id} ===")
        print(f"  data window: {report.test.data_window}")
        print(f"  train: {len(report.train.trades)} trades, {report.train.total_return_pct:+.2%} return, Sharpe {report.train.sharpe:.2f}")
        print(f"  validate: {len(report.validate.trades)} trades, {report.validate.total_return_pct:+.2%} return, Sharpe {report.validate.sharpe:.2f}")
        print(f"  test: {len(report.test.trades)} trades, {report.test.total_return_pct:+.2%} return, Sharpe {report.test.sharpe:.2f}")
        print(f"  deflated Sharpe: {report.deflated_sharpe:.2f}")
        print(f"  parameter sensitivity: {report.parameter_sensitivity}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run against the real, backfilled ledger and record the report**

This step requires Task 5's backfill to have actually been run against the real ledger first (`python scripts/backfill_history.py`) — a real network operation against Hyperliquid's API, not something to fake in a test. Run it, then:

Run: `C:\ProgramData\Anaconda3\python.exe scripts/run_seed_backtests.py`
Expected: completes in well under a minute per spec; prints a report for all 3 specs. Save the output into this task's report (or a new `docs/superpowers/reports/2026-07-07-seed-backtest-results.md`) so the actual historical profiles are recorded, not just produced and discarded.

- [ ] **Step 7: Commit**

```bash
git add agents/specs/ scripts/run_seed_backtests.py tests/test_seed_specs.py
git commit -m "feat(backtest): hand-compile 3 seed specs and add the seed-backtest runner"
```

**Definition of done:** `backtest(spec, ...)` for all 3 seed specs returns an equity curve + overfit report in under a minute per spec; each spec has a known historical profile, honestly scoped to the real data window each of its evidence terms actually had.

---

## Execution notes

Task order: 1 and 2 can run in parallel (both touch `market/heartbeat.py` but disjoint sections — sequence them if run by the same implementer to avoid a merge conflict, parallel-safe across two implementers). 3 and 4 depend on nothing but each other in sequence (dsl before interpreter). 5 is independent of 3/4. 6 depends on 2, 3, 4. 7 depends on 6. 8 depends on 3, 4, 6, 7, and requires 5 to have actually been run once against the real ledger before Step 6 produces a meaningful report.

After all tasks: run the full suite once —
`C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py`
— before considering the plan complete.
