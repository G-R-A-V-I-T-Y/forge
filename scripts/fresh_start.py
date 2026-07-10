"""scripts/fresh_start.py -- Reset the desk and seed all 10 initial agents.

WARNING: This deletes ALL existing data in the database.
Run only when you want a clean start.
"""
import shutil
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from backtest.dsl import load_spec
from meta.spawner import spawn_agent
from store.db import get_connection, init_schema
from store.specs import SPECS_DIR, deploy_spec

DB_PATH = PROJECT_ROOT / "data" / "forge.db"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _load_starting_balance() -> float:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return float(cfg.get("desk", {}).get("starting_balance", 50000.0))


def _load_desk_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return cfg.get("desk", {})


# M8 (Evolution): per-agent config_overrides applied at seed time.
#
# - iron_moth, silver_basin, jade_hawk are the 3 seed agents with a
#   hand-compiled DSL spec available (agents/specs/{name}_v1.yaml, from
#   M7b). Marking them `compiled: True` routes their decision loop through
#   backtest/interpreter.py against the live heartbeat feature row instead
#   of calling the LLM (see agents/decision_loop.py). Their specs are
#   deployed into the `specs` table below, right after spawning, via
#   store/specs.py's deploy_spec().
# - sage_turtle (event/unlock positioning) is spawned separately by
#   forge.py at startup with its own compiled config -- not part of this
#   seed list.
# - copper_vane, steel_crane, onyx_heron are designated the pure-LLM
#   control arm: pinned to a single fixed model via config_json's
#   `pinned_model` key -- the only per-agent model-pinning mechanism that
#   actually exists in this codebase (see llm/model_chain.py's
#   `_get_agent_pinned_model()`, which reads exactly this key). There is
#   no per-agent "temperature" knob anywhere in the LLM client stack
#   (llm/client.py, llm/model_chain.py, llm/ollama_client.py,
#   llm/llama_server_client.py all call their backends with no
#   temperature parameter), so one is deliberately not invented here --
#   doing so would silently do nothing since nothing reads it.
# - violet_lion and crimson_fox are left with no overrides: pure-LLM,
#   default fallback chain, not part of the fixed-model control-arm
#   comparison set.
#
# Net roster split after forge.py additionally retires gray_finch/amber_wolf
# and spawns sage_turtle: 4 compiled (iron_moth, silver_basin, jade_hawk,
# sage_turtle) + 3 fixed-model control-arm (copper_vane, steel_crane,
# onyx_heron) + 2 default pure-LLM (violet_lion, crimson_fox) = 9 active
# agents. This lands close to, but not exactly on, the milestone's
# 6-7 compiled / 2-3 control-arm split -- only 3 seed agents have
# hand-compiled specs available today, so 3 (+sage_turtle=4) compiled is
# what the current spec roster supports.
_CONTROL_ARM_MODEL = "openrouter/anthropic/claude-sonnet-5"

CONFIG_OVERRIDES: dict[str, dict] = {
    "iron_moth": {"compiled": True},
    "silver_basin": {"compiled": True},
    "jade_hawk": {"compiled": True},
    "copper_vane": {"pinned_model": _CONTROL_ARM_MODEL},
    "steel_crane": {"pinned_model": _CONTROL_ARM_MODEL},
    "onyx_heron": {"pinned_model": _CONTROL_ARM_MODEL},
}

# Agents with a hand-compiled spec on disk (agents/specs/{name}_v1.yaml)
# that should be deployed into the `specs` table right after seeding so
# store.specs.get_active_spec() returns something for their compiled
# decision-loop path.
COMPILED_SPEC_AGENTS = ["iron_moth", "silver_basin", "jade_hawk"]

# sage_turtle's thesis lives here (not in forge.py) so all seed data is
# centralised in one file.
_SAGE_TURTLE_THESIS = """\
# sage_turtle -- Thesis v1: Event & Unlock Positioning

## Edge Hypothesis

Scheduled supply and macro events are public, dated, and repeatedly under-anticipated by the market until they are imminent. Token unlocks release a known quantity of new supply to holders (often early investors/team with a low cost basis and a high propensity to sell) at a known timestamp; the market chronically underprices the sell pressure until the unlock is within days, then overcorrects. Macro events (FOMC, CPI) do not move any single asset's supply, but they reset the funding/leverage backdrop for the entire book in ways theses cannot see coming. This agent does not predict price from price -- it predicts price from the calendar: what is scheduled, how large is it relative to float, and how has the market historically reacted to this specific event type.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule -- wait, but log to the watchlist if an event is within 10 days

## Position Parameters

- Direction: Short into large unlocks (dilution); long only in the rare case of a documented buyback/burn event with equivalent evidence structure inverted.
- Leverage: 3x
- Position size: 10% of account per trade
- Stop loss: 3.0% from entry
- Take profit: 6.0% from entry
- Max hold time: through the event plus 24 hours, then exit regardless of P&L
"""

# All agents that carry a compiled spec (seed + sage_turtle).
_COMPILED_AGENTS = [*COMPILED_SPEC_AGENTS, "sage_turtle"]


SEED_AGENTS = [
    (
        "iron_moth",
        """# iron_moth -- Thesis v1: Cross-sectional Momentum

## Edge Hypothesis

Cross-sectional momentum across the perpetuals universe produces persistent, uncorrelated alpha that is independent of absolute market direction. By ranking all assets in the universe on multiple return horizons (30m, 2h, 12h, 24h) and entering the strongest relative performers, the agent captures the tendency of winning assets to continue outperforming losers within the same asset class -- the textbook momentum premium adapted for crypto perps.

Momentum alone is noisy. The edge is sharpened by requiring momentum acceleration (the most recent horizon > longer horizons) and volatility-adjusted returns (Sharpe ratio of the momentum signal over the lookback). Sector-relative momentum excludes trades that are just beta -- if all assets are rising, the agent prioritises assets rising the most within their sector peer group. Funding rate and order book data are explicitly ignored to keep the signal pure.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Composite momentum rank**: Asset rank in the universe by weighted momentum score. Top 3 = strong +0.7, rank 4-5 = moderate +0.4, rank 6-10 = weak +0.1. Outside the top 10, the signal is too diluted to contribute meaningful conviction (+0.0).
- **Momentum acceleration**: Most recent horizon return exceeding the average of all longer horizons. Clear acceleration adds +0.5. Flat acceleration (recent horizon roughly equal to longer horizons) adds +0.2. Deceleration (recent horizon below longer averages) reduces confidence by -0.4 — this is a warning the momentum is fading.
- **Volatility-adjusted return**: Return divided by ATR over the lookback. Ratio > 0.5 = strong +0.6, 0.3-0.5 = moderate +0.3, 0.15-0.3 = weak +0.1. Below 0.15 the signal is noise-dominated and contributes nothing.
- **Sector-relative rank**: Top 2 within the asset's sector peer group adds +0.4. Rank 3-4 within sector adds +0.2. Bottom of the sector (despite high absolute rank) reduces by -0.3 — the asset is riding beta, not alpha.

### Secondary Evidence (moderate weight)

- **BTC correlation discount**: If the asset is not the highest-correlation member of its sector to BTC, add +0.2 (idiosyncratic momentum, not beta). If it is the highest-correlation member, reduce by -0.2.
- **Volume confirmation**: Volume above the 14-day median adds +0.2. Below the median reduces by -0.2 (momentum without participation is suspect).
- **Regime compatibility**: No conflicting regime tag (e.g. avoid fresh crisis regime) adds +0.1. Crisis or range_high_vol regimes reduce momentum reliability by -0.3.

### When Data Is Missing

If return data for one or more horizons is unavailable for a given asset, exclude that horizon from the composite score and note the reduction. If two or more horizons are missing, the asset cannot be reliably ranked — remove it from the universe for that cycle. Volume data missing defaults to neutral (0.0 instead of the secondary contribution). If sector classification is unavailable, skip the sector-relative rank check entirely and apply a -0.1 uncertainty penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Long only (cross-sectional momentum on perps favors
  the long side due to funding cost on shorts)
- Leverage: 3x
- Position size: 12% of account per trade
- Stop loss: 2.5% below entry price
- Take profit: 5.0% above entry price (2:1 reward/risk)
- Max hold time: 12 hours; if TP not hit, re-evaluate at next wake

## Known Weaknesses

- Underperforms during sharp trend reversals when momentum flips
- High correlation to other long-biased strategies on the desk
- Mean-reversion regimes (range_high_vol) generate whipsaws
- In low-vol regimes the ranking becomes noise-dominated

## Assets in Focus

Primary: SOL, ETH, SUI, AVAX, LINK (higher momentum dispersion)
Secondary: BTC (lower momentum variance but anchor position)
Avoid: Stablecoin pairs and assets with <$50M daily volume
""",
    ),
    (
        "silver_basin",
        """# silver_basin -- Thesis v1: Funding Dislocation

## Edge Hypothesis

Funding rates are the purest expression of leverage demand in the crypto perpetuals market. When traders are willing to pay extreme premiums (positive funding) or receive extreme discounts (negative funding), they are expressing a conviction that is statistically likely to be wrong at the extreme. Funding dislocations are self-correcting: the cost of holding a trade mechanically reduces the edge of the crowded side until it unwinds.

This agent studies nothing but funding. It computes the current funding rate z-score vs a rolling 14-day history, the funding trend (direction of the last 3 periods), predicted funding from OI changes (when OI surges, funding tends to follow), and funding acceleration (the rate of change of funding itself). Entry is triggered when funding is statistically irrational -- extreme z-scores combined with accelerating trend -- and held until funding normalises. Price action is deliberately excluded to avoid confirmation bias.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Funding rate extremity (z-score)**: The distance of the current funding rate from its 14-day mean, measured in standard deviations. z-score > 2.0 or < -2.0 = strong +0.7, 1.5-2.0 = moderate +0.5, 1.0-1.5 = weak +0.2. Below 1.0 the funding rate is within normal range — contribute 0.0 for dislocation signals. The sign of the z-score determines direction: positive extreme = short signal, negative extreme = long signal.
- **Funding acceleration**: The rate of change of funding in the direction of the dislocation. Acceleration in the last period matching the dislocation direction adds +0.5. Flat acceleration (last period funding is similar to prior) adds +0.2. Deceleration (funding starting to revert) reduces confidence by -0.4 — the best entry may have passed.
- **OI-funding alignment**: OI change supporting the funding thesis. For positive funding extreme: OI flat or falling (late-stage crowding) adds +0.3; OI still rising (still building) adds +0.1 but warns the dislocation may have further to run. For negative funding extreme: OI stable or rising (building shorts) adds +0.3; OI falling adds +0.1. OI data unavailable: treat as neutral but apply -0.1 uncertainty penalty.

### Secondary Evidence (moderate weight)

- **Persistence of extreme**: Funding has been extreme for 2+ consecutive funding periods adds +0.3 (confirms the dislocation is structural, not a one-period anomaly). Single-period extreme contributes nothing.
- **Predicted funding agreement**: The predicted funding from the OI model agreeing with the current extreme adds +0.2. Disagreement reduces by -0.2 (the OI model suggests the extreme may not persist).
- **Idiosyncratic check**: Other assets in the same sector showing normal funding adds +0.2 (idiosyncratic dislocation is more likely to revert quickly). Sector-wide funding dislocation reduces by -0.2 (systematic positioning, slower to revert).
- **Event calendar check**: No major scheduled event for the asset in the next 4 hours adds +0.1. A known event within 4 hours reduces by -0.4 (events override funding dynamics).

### When Data Is Missing

If funding rate data is unavailable for the current period, do not enter — funding is the sole signal for this thesis. If fewer than 14 days of funding history are available, use whatever history exists with a -0.1 uncertainty penalty per missing day below 14 (maximum -0.5). If OI data is unavailable, skip the OI-funding alignment check entirely (treat as 0.0) and apply the uncertainty penalty noted above. Predicted funding is a derived signal: if it cannot be computed, skip it with no penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: For positive extreme: Short (fade the long premium).
  For negative extreme: Long (fade the short premium)
- Leverage: 4x (higher leverage because mean reversion is time-bound
  by funding settlement)
- Position size: 10% of account per trade
- Stop loss: 2.0% from entry (tight -- if funding doesn't revert soon,
  the thesis is wrong)
- Take profit: Exit when funding z-score returns to within ±1.0
  (the signal resolves, not a price target)
- Max hold time: Until next funding settlement (max 8 hours)

## Known Weaknesses

- In persistent trends, funding can stay extreme for days -- this
  agent bleeds to the trend
- Most correlated to `jade_hawk` (VWAP mean reversion) -- both fade
  extremes, jade_hawk from price and this agent from funding
- Low volatility regimes with stable funding produce no signals
- Gap risk: funding normalises via a sharp move that hits the SL
  first, then continues in the thesis direction

## Assets in Focus

Primary: SOL, ETH, ARB, OP, SUI (high funding variance)
Secondary: BTC (lower funding variance but deeper liquidity)
Avoid: PEPE, DOGE, WIF (funding too noisy, low predictive value)
""",
    ),
    (
        "copper_vane",
        """# copper_vane -- Thesis v1: Open Interest Intelligence

## Edge Hypothesis

Open interest reveals conviction behind price moves better than price alone. The OI×Price regime matrix classifies market states into four categories, each with a clear directional implication:

- Rising price + rising OI = genuine new participation. Capital is
  entering the asset, longs and shorts are both adding. This is
  a healthy trend -- go with it.
- Rising price + falling OI = short squeeze or distribution. Price
  is rising but total exposure is shrinking -- shorts are being
  forced out or smart money is distributing. Fade the move.
- Falling price + rising OI = aggressive new shorts. Capital is
  entering on the short side, indicating informed selling. Join it.
- Falling price + falling OI = longs giving up. Capitulation without
  new short conviction -- wait for reversal.

OI data from Hyperliquid updates every few seconds and reflects real on-chain positioning. This signal is difficult to spoof and leads price, especially at turning points. The agent ignores raw price action as a primary signal and classifies every 4-hour window into one of the four OI×Price quadrants, entering only when the classification is unambiguous.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **OI×Price regime classification**: A clear, sustained classification over the full 4-hour window contributes +0.5. Ambiguous regimes (flipping between quadrants) reduce confidence by -0.3; the regime must be resolved before entry is viable.
- **OI change magnitude**: Stronger signals at larger values (>3% = strong +0.8, 1.5-3% = moderate +0.5, <1% = weak +0.2). Direction of change determines sign: rising OI is positive for rising price setups, negative for falling price setups.
- **Price change magnitude**: Stronger moves carry more conviction (>1.5% = strong +0.6, 0.5-1.5% = moderate +0.3, <0.5% = weak +0.1). Sign must align with OI direction for the classification to hold.
- **Prior window consistency**: If the prior 4-hour window shows a conflicting OI regime, reduce confidence by 30-40%; if consistent, maintain full conviction.

### Secondary Evidence (moderate weight)

- **Volume confirmation**: Volume direction matching OI direction adds +0.3; opposing direction reduces by -0.2.
- **Funding rate support**: Funding neutral or supporting OI thesis adds +0.2; strongly opposing funding reduces confidence by -0.3.
- **Sector-wide OI change**: Broad OI movement across the sector adds +0.2 to conviction; isolated OI in a single asset adds nothing.

### When Data Is Missing

If OI data is unavailable, the regime classification and OI change pillars are removed: maximum achievable confidence drops to ~40%. No entry is warranted in this state — the thesis has no edge without OI data. If price data is available but lagging, use 1-minute snapshots instead of real-time; treat staleness as a -0.2 uncertainty penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Determined by OI×Price quadrant classification.
   Rising+Rising = Long. Rising+Falling = Short. Falling+Rising =
   Short. Falling+Falling = Wait (no entry).
- Leverage: 3x
- Position size: 12% of account per trade
- Stop loss: 2.0% from entry
- Take profit: 4.0% from entry (2:1 reward/risk)
- Max hold time: 8 hours

## Known Weaknesses

- In low-volume regimes, OI changes are small and classification
  becomes unreliable
- OI data can lag during high-volatility events on Hyperliquid
- After large liquidation cascades, OI drops mechanically -- this
  creates false 'falling+falling' signals
- Most effective on mid-cap perps (SOL, ARB, SUI) where OI
  changes are driven by conviction, not passive hedging

## Assets in Focus

Primary: SOL, ETH, SUI, ARB, OP (good OI depth)
Secondary: BTC, BNB (deep OI but lower predictive variance)
Avoid: Assets with <$10M OI (noise-dominated)
""",
    ),
    (
        "gray_finch",
        """# gray_finch -- Thesis v1: Order Book Microstructure

## Edge Hypothesis

Over short time horizons (5-20 minutes), order book microstructure predicts imminent price direction more reliably than any lagging indicator based on OHLCV data. The book shows exactly where supply and demand sit, who is willing to pay what, and where the market will encounter resistance or support in the next few ticks.

This agent computes bid/ask imbalance (the ratio of total bid size to total ask size across the top 10 levels), identifies liquidity gaps between price levels (zones with thin book depth where price can move quickly), detects resting walls (clusters of large limit orders that act as magnets or resistance), tracks spread width relative to its recent history, and estimates expected slippage for a position of the agent's target size. Queue dynamics measure whether the inside bid or ask is being eaten through or replenished. The agent never looks at 24h charts, funding rates, or any macro-level data -- the microstructure is the complete picture at the decision horizon.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Directional imbalance**: Bid/ask ratio across the top 10 levels. For long entries: ratio > 1.5 = strong +0.7, 1.2-1.5 = moderate +0.4, 1.0-1.2 = weak +0.1. For short entries: ratio < 0.67 = strong +0.7, 0.67-0.85 = moderate +0.4, 0.85-1.0 = weak +0.1. Ratios near 1.0 contribute near-zero directional conviction.
- **Resting wall proximity**: No resting wall within 0.5% of current price in the entry direction is ideal (+0.0 penalty). A wall within 0.5% reduces confidence by -0.5; a wall within 0.25% is a near-certain stopper, reducing by -0.8. If the wall is on the opposite side of the book, it may act as support — add +0.2.
- **Liquidity gap existence**: A liquidity gap within 1% of current price in the entry direction adds +0.5. A gap within 0.5% adds +0.7 (clean path for price movement). No gap within 1% reduces confidence by -0.3 (price will fight through stacked book depth).

### Secondary Evidence (moderate weight)

- **Expected slippage**: Slippage estimate for target size. Slippage < 0.05% adds +0.3; 0.05-0.1% adds +0.1; 0.1-0.2% reduces by -0.2; > 0.2% reduces by -0.5 (cost destroys edge).
- **Inside queue dynamics**: Queue being consumed aggressively (not replenished) in the entry direction adds +0.3. Queue being replenished faster than consumed reduces by -0.2. Neutral queue gets 0.0.
- **Spread tightness**: Spread tighter than the 20-period moving average adds +0.2 (active liquid market). Wider than average reduces by -0.2 (stale or gappy book).
- **Recent trade direction**: Small trades (1-5 contracts) predominantly on the aggressive side of the entry direction adds +0.2. Mixed or opposing trade flow reduces by -0.1.

### When Data Is Missing

Order book data is the foundation of this thesis — if the top 10 levels of the book are unavailable or stale by more than 1 second, do not enter. The microstructure signal decays too quickly to use cached data. Partial book data (e.g. only top 5 levels) reduces the effective imbalance calculation: treat the available levels as the full picture but apply a -0.2 uncertainty penalty. If slippage estimation is unavailable, assume the midpoint of the range (0.1%) and apply a -0.1 uncertainty penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Determined by microstructure imbalance
- Leverage: 4x (short hold times reduce gap risk)
- Position size: 8% of account per trade (smaller because of
  higher frequency and shorter horizons)
- Stop loss: 1.0% from entry (tight -- microstructure signal decays
  quickly if wrong)
- Take profit: 2.0% from entry (2:1 reward/risk for quick scalps)
- Max hold time: 20 minutes; if TP not hit, exit on schedule

## Known Weaknesses

- Highly sensitive to book quality -- during low liquidity periods
  (e.g. weekends on altcoins), microstructure signals are noise
- Transaction costs (spread + slippage) consume a meaningful
  fraction of edge at this horizon -- requires low-fee venue
- News events overwhelm microstructure completely -- flat before
  known events
- Most correlated to `amber_wolf` (trade flow) -- microstructure
  and flow are two sides of the same book

## Assets in Focus

Primary: BTC, ETH (deepest books, best microstructure signal)
Secondary: SOL (good depth, consistent patterns)
Avoid: Low-liquidity perps (PEPE, WIF, TRUMP) -- book too thin
""",
    ),
    (
        "amber_wolf",
        """# amber_wolf -- Thesis v1: Trade Flow

## Edge Hypothesis

Every market transaction carries information. Aggressive buyers (market buys lifting the ask) and aggressive sellers (market sells hitting the bid) reveal the direction of informed flow. By analysing every execution in real-time, the agent can detect when institutional or otherwise informed capital is moving directionally before that flow is fully reflected in the mid-price.

Key metrics: aggressive buy volume vs aggressive sell volume over rolling windows (1m, 5m, 15m), VWAP relative to the mid-price (informed flow pays the spread -- elevated VWAP above mid confirms buying pressure), buy pressure ratio (agg buys / total agg volume), average trade size (block trades vs retail -- a surge in average size signals institutional participation), and cumulative delta (agg buys minus agg sells over a window). The signature of informed flow is overwhelming one-sided aggression at above-average trade size, sustained across multiple time windows.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Buy pressure ratio**: Aggressive buy volume divided by total aggressive volume across the 5-minute window. For long entries: ratio > 0.70 = strong +0.7, 0.60-0.70 = moderate +0.5, 0.55-0.60 = weak +0.2. For short entries: ratio < 0.30 = strong +0.7, 0.30-0.40 = moderate +0.5, 0.40-0.45 = weak +0.2. Ratios near 0.50 contribute near-zero directional conviction.
- **Average trade size anomaly**: Average trade size relative to the 1-hour rolling average. > 2.0x = strong +0.6 (clear institutional participation), 1.5-2.0x = moderate +0.4, 1.2-1.5x = weak +0.1. Below 1.2x the participant profile is retail noise — contribute 0.0.
- **Cumulative delta consistency**: Cumulative delta directionally consistent across all three windows (1m, 5m, 15m) adds +0.5. Two of three windows aligned adds +0.3. One window aligned (divergent otherwise) adds +0.1. No alignment (accumulation-distribution pattern, sawtooth delta) reduces by -0.4 — this is absorption, not accumulation.

### Secondary Evidence (moderate weight)

- **VWAP deviation from mid**: Deviation > 0.10% in the direction of flow adds +0.3 (aggressors paying up confirms conviction). Deviation 0.05-0.10% adds +0.1. Below 0.05% contributes nothing. Deviation opposing flow direction reduces by -0.3.
- **Funding rate support**: Funding rate supporting the flow direction adds +0.2 (flow + funding converging = high conviction). Opposing funding reduces by -0.2. Neutral funding adds nothing.
- **Order book imbalance**: Book imbalance (from gray_finch style micro reading) agreeing with flow direction adds +0.2. Book disagreeing reduces by -0.2. Book data unavailable: skip with no penalty.
- **Volume acceleration**: Volume accelerating into the move (1m > 5m > 15m on a per-minute normalised basis) adds +0.3. Flat or decelerating volume profile adds 0.0.

### When Data Is Missing

If trade flow data for the 5-minute window is unavailable (missed execution feed ticks), do not enter — flow is the sole foundation of this thesis and stale data is worthless. If the 1-hour rolling average for trade size is unavailable (not enough history since agent start), use the available shorter history with a -0.1 uncertainty penalty per missing hour below 1 hour (maximum -0.3). If VWAP data is unavailable, skip the VWAP deviation check with no penalty. If book imbalance data is unavailable, skip with no penalty. If funding rate data is unavailable, skip with no penalty. If cumulative delta can only be computed on a subset of the windows (e.g. only 5m and 15m available), treat as two-of-three alignment with the available windows.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Direction of aggressive flow
- Leverage: 3x
- Position size: 10% of account per trade
- Stop loss: 1.5% from entry
- Take profit: 3.0% from entry (2:1 reward/risk)
- Max hold time: 30 minutes -- flow signals decay; if the move
  hasn't materialised in 30m, the flow was likely absorption, not
  accumulation

## Known Weaknesses

- In heavily algorithmic markets (BTC), trade flow can be
  strategically spoofed or split across multiple venues
- During low-volume periods, average trade size is unreliable
- Flow analysis works best on mid-cap perps where institutional
  flow is concentrated enough to measure
- Correlated to `gray_finch` during high-volume regimes -- both
  read the same book from different angles

## Assets in Focus

Primary: SOL, SUI, ARB, OP (concentrated institutional flow)
Secondary: BTC, ETH (deep flow but higher signal-to-noise ratio
  required)
Avoid: Meme coins (PEPE, WIF, DOGE) -- retail-dominated flow
""",
    ),
    (
        "steel_crane",
        """# steel_crane -- Thesis v1: Liquidation Hunter

## Edge Hypothesis

Crypto perpetual markets generate a unique and exploitable signal: forced liquidations. When leveraged positions are forcibly closed, the mechanical cascade creates temporary price dislocations that are both larger and more predictable than the efficient market hypothesis would predict. Liquidations create the opposite signal: long liquidations create downside pressure and then a vacuum that pulls price back; short liquidations create upside fuel.

This agent monitors liquidation clusters (concentrated volumes at specific price levels), cascade history (chain liquidations that snowball as each liquidation pushes price further), leverage estimates from OI/price ratios (when estimated leverage is extreme, liquidations become more likely), funding rates (trapped longs paying high funding while underwater are prime cascade targets), and OI changes in the wake of liquidation events. The core question: is a squeeze likely? Has one already happened? If the cascade is in progress, the agent fades it -- enters against the cascade direction expecting reversion as the book rebalances.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Liquidation volume magnitude**: Single-asset liquidation volume in the last 15 minutes. > $10M = strong +0.8, $5-10M = moderate +0.6, $2-5M = weak +0.3. Below $2M the cascade signal is too small to reliably fade — contribute 0.0. Volume direction determines sign: long liquidations = short-term downside, then fade long.
- **Price move magnitude**: Price movement in the direction of the dominant liquidation type. > 3% = strong +0.6, 2-3% = moderate +0.4, 1-2% = weak +0.2. Below 1% the price hasn't reacted to the liquidations — contributes 0.0.
- **Estimated leverage**: Current estimated leverage (OI / notional value) for the asset. > 20x = strong +0.5, 15-20x = moderate +0.3, 10-15x = weak +0.1. Below 10x contributes nothing. Higher leverage means more fuel remains for the cascade.
- **OI drawdown during cascade**: OI drop during the cascade event. > 5% drop = strong +0.6 (significant positions removed), 3-5% drop = moderate +0.4, 1-3% drop = weak +0.1. Below 1% OI change suggests the cascade is not materially reducing open positions — reduce by -0.2.

### Secondary Evidence (moderate weight)

- **Pre-cascade funding extremity**: Funding rate was extreme (z-score > 1.5 or < -1.5) before the cascade adds +0.3. Trapped positions paying to stay in are prime cascade fuel. Neutral pre-cascade funding contributes nothing.
- **Liquidation cluster concentration**: Multiple liquidation clusters visible on the book adds +0.3 (cascades propagate through clustered levels). A single liquidation level adds +0.1. No visible clusters reduce by -0.2.
- **Idiosyncratic check**: Other assets in the same sector are not experiencing cascades adds +0.2 (idiosyncratic event, safer to fade). Sector-wide cascades reduce by -0.3 (systemic risk overwhelms the fade thesis).
- **Regime compatibility**: Regime tag is not crisis adds +0.2. Crisis regime reduces by -0.5 — systemic cascades behave differently and the fade is much riskier.

### When Data Is Missing

If liquidation volume data is unavailable, this thesis has no edge — do not enter. If OI data is unavailable, the OI drawdown and estimated leverage pillars are removed: maximum achievable confidence drops to ~50%. If funding rate data is unavailable, skip the pre-cascade funding check with no penalty. If liquidation cluster data is unavailable (book data missing), skip with no penalty. If regime tag is unavailable, assume non-crisis (most conservative assumption) with no penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Counter-cascade. Long liquidations → Long.
  Short liquidations → Short.
- Leverage: 4x (higher leverage to compensate for rare-event
  frequency -- cascades happen infrequently but have high edge)
- Position size: 8% per trade (conservative -- cascade timing is
  notoriously difficult)
- Stop loss: 2.0% from entry
- Take profit: 4.0% from entry (2:1 reward/risk)
- Max hold time: 4 hours (cascades resolve quickly; if the
  reversion hasn't happened in 4h, the thesis was wrong)

## Known Weaknesses

- Cascades can compound (cascading liquidations that trigger more
  liquidations) -- fading early gets run over
- False cascades: a large single liquidation can look like a
  cascade; need cluster detection to distinguish
- Most effective on mid-cap perps (SOL, SUI, ARB) where liquidation
  impact on price is highest
- BTC cascades are deeper but fade more quickly -- tighter edge

## Assets in Focus

Primary: SOL, SUI, ARB, OP, ETH (active liquidation markets)
Secondary: BTC (deep but spreads edge thin)
Avoid: Low-OI assets with <$10M daily liquidation volume
""",
    ),
    (
        "onyx_heron",
        """# onyx_heron -- Thesis v1: Relative Value

## Edge Hypothesis

Within the crypto perpetuals universe, assets in the same sector or with structural relationships (SOL vs ETH, BTC vs ETH, AI-token basket vs Layer-1 basket) diverge and converge in statistically predictable patterns. These spreads are driven by temporary capital flows, narrative shifts, and leverage dynamics that mean-revert over hours to days. Trading the spread removes the need to predict absolute market direction -- the edge comes from correctly identifying when one asset is statistically cheap or rich relative to another.

The agent monitors a fixed set of pairs and baskets using z-score of the spread vs a rolling window, rolling correlation (assets must remain structurally related for the thesis to hold), and cointegration tests (pairs that drift apart must tend to revert, not diverge permanently). Entry is triggered when a spread is extreme (z-score > 2.0 or < -2.0) and cointegration is confirmed. The agent is always long the cheap leg and short the rich leg, reducing market beta to near zero when sized correctly.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Spread extremity (z-score)**: The distance of the current spread from its mean, measured in standard deviations. z-score > 2.0 or < -2.0 = strong +0.7, 1.5-2.0 = moderate +0.5, 1.0-1.5 = weak +0.2. Below 1.0 the spread is within normal noise — contribute 0.0. The sign of the z-score determines which leg is rich and which is cheap.
- **Rolling correlation**: 20-period correlation between the two assets. > 0.80 = strong +0.6, 0.60-0.80 = moderate +0.4, 0.40-0.60 = weak +0.1. Below 0.40 the structural relationship is questionable — reduce by -0.4 and flag for re-evaluation. Correlation is checked but treated as a continuous confidence input rather than a hard gate.
- **Cointegration test**: p-value of the cointegration test. p < 0.01 = strong +0.6, p < 0.05 = moderate +0.4, p < 0.10 = weak +0.1. p >= 0.10 means the spread may be a random walk — reduce conviction by -0.5 and strongly consider skipping the trade. The cointegration refresh window matters: a stale test (> 7 days old) is treated as p < 0.10 regardless of the stored value.
- **Fundamental catalyst check**: If the spread extreme can be clearly attributed to a known fundamental catalyst on one leg (SOL network outage, ETF news, major unlock), reduce confidence by -0.7 — this is a regime change, not a mean-reversion setup. If no catalyst is found, add +0.2 (the move is technical, not fundamental). If the check cannot be performed (news unavailable), apply a -0.1 uncertainty penalty.

### Secondary Evidence (moderate weight)

- **Funding rate neutrality**: Funding rates on both legs are within normal range (z-score within ±1.0) adds +0.3. If either leg shows extreme funding, reduce by -0.3 (artificial pressure on the spread).
- **Regime compatibility**: Range_low_vol or range_high_vol regime adds +0.3. Trending regime reduces by -0.3 (trends can keep spreads extreme for weeks).
- **Peer spread normality**: Other pairs in the same sector showing normal spreads adds +0.2 (idiosyncratic dislocation is more likely to revert). If the whole sector is dislocated, reduce by -0.2 (sector-wide repricing, not a pair trade setup).
- **Persistence of extreme**: Spread has been extreme for 2+ hours adds +0.2 (confirms the regime hasn't changed mid-evaluation). Less than 2 hours of persistence contributes nothing. More than 48 hours of extreme spread reduces by -0.3 (the relationship may have structurally changed).

### When Data Is Missing

If price data for one leg of the pair is stale by more than 5 minutes, do not enter — stale pricing on either leg invalidates the spread calculation. If correlation data is under the minimum observation window (fewer than 20 periods for the rolling correlation), use the available periods with a -0.2 uncertainty penalty. If cointegration test results are unavailable or too old to use, treat as p >= 0.10 (the cautious assumption) with a -0.2 uncertainty penalty. If a fundamental catalyst check cannot be performed, proceed with the uncertainty penalty described above.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Long the cheap leg, short the rich leg (always pair)
- Leverage: 2x per leg, 4x total notional for the pair
- Position size: 8% per leg (16% total notional per pair trade)
- Stop loss: Spread widens by 2x the entry z-score (structural
  break, not a routine retracement)
- Take profit: Spread z-score returns to within ±0.5
- Max hold time: 7 days -- some spreads take days to converge;
  re-evaluate daily

## Known Weaknesses

- Structural break risk: a previously cointegrated pair can
  permanently diverge (e.g. ETH after the merge vs BTC)
- Funding cost on the short leg can be significant over multi-day
  holds -- must factor into expected value
- In trending regimes (trending_bull, trending_bear), the spread
  can remain extreme for weeks
- Requires two legs to be simultaneously liquid -- limits universe

## Pairs in Focus

Primary: SOL/ETH, ETH/BTC, SOL/BTC, AI-basket/L1-basket
Secondary: ARB/OP, SUI/APT, LINK/AAVE (sector pairs)
Avoid: Meme coins against blue chips (no structural relationship)
""",
    ),
    (
        "jade_hawk",
        """# jade_hawk -- Thesis v1: VWAP Mean Reversion

## Edge Hypothesis

Price consistently overshoots and reverts to VWAP across multiple timeframes, especially at statistical extremes. VWAP acts as a gravitational centre for price: institutions execute large orders algorithmically around VWAP, market-makers hedge around it, and the settlement mechanics of perps create natural reversion pressure. When price deviates significantly from VWAP -- more than two ATRs away on a 15-minute chart -- the probability of reversion within the next 1-4 hours increases well above random.

This agent fades price extremes relative to VWAP. It does not predict direction -- it predicts that price is statistically stretched and will revert toward the mean. Every momentum agent on the desk buys strength; jade_hawk sells it and buys weakness. This negative correlation to the desk's trend strategies provides natural diversification.

The signal is strongest when multiple timeframe VWAPs (15m, 1h, 4h) all show deviation in the same direction, confirming that the move is extended on multiple execution horizons. Volume confirmation distinguishes a climax (reversion likely) from a genuine breakout (momentum likely).

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

#### For short entries (price overextended above VWAP):
- **VWAP deviation magnitude (1h)**: Distance of current price above VWAP(1h). > 2.0 ATR = strong +0.7, 1.5-2.0 ATR = moderate +0.5, 1.0-1.5 ATR = weak +0.2. Below 1.0 ATR the deviation is too small to fade — contribute 0.0.
- **Multi-timeframe confirmation**: VWAP(4h) also below current price adds +0.4. VWAP(4h) above current price (conflicting) reduces confidence by -0.3. No VWAP(4h) data available: treat as neutral but apply -0.1 uncertainty penalty.
- **Volume climax signal**: Volume > 1.5x the 14-period average on the 15m chart adds +0.5. Volume 1.0-1.5x average adds +0.2. Volume below average (quiet drift, not climax) reduces by -0.3 — the extension lacks a capitulation signature.
- **Sustained extension**: Move sustained for 3+ consecutive 15m candles adds +0.3 (not a single wick). 1-2 candles adds +0.1. A single-candle wick with no follow-through contributes nothing.

#### For long entries (price compressed below VWAP):
- **VWAP deviation magnitude (1h)**: Distance of current price below VWAP(1h). > 2.0 ATR = strong +0.7, 1.5-2.0 ATR = moderate +0.5, 1.0-1.5 ATR = weak +0.2.
- **Multi-timeframe confirmation**: VWAP(4h) also above current price adds +0.4. VWAP(4h) below current price reduces by -0.3.
- **Volume climax signal**: Volume > 1.5x average adds +0.5; 1.0-1.5x adds +0.2; below average reduces by -0.3.
- **Sustained compression**: Move compressed for 3+ consecutive 15m candles adds +0.3; 1-2 candles adds +0.1.

### Secondary Evidence (moderate weight)

- **RSI confirmation**: RSI(14) on 1h chart > 70 (short) or < 30 (long) adds +0.3. RSI 60-70 or 30-40 adds +0.1. RSI near 50 contributes nothing.
- **Funding rate support**: Extreme positive funding + price above VWAP = strong short signal, add +0.3. Extreme negative funding + price below VWAP = strong long signal, add +0.3. Neutral or opposing funding reduces by -0.2.
- **Regime compatibility**: Range_low_vol or range_high_vol regime adds +0.3 (mean-reversion friendly). Trending regime reduces by -0.5 (trend can keep price extended indefinitely).
- **Event calendar clean**: No major scheduled event for the asset in the next 4 hours adds +0.1. Known event within 4 hours reduces by -0.4.
- **Not liquidation-driven**: Confirmation that the move is not driven by a liquidation cascade adds +0.2. If a cascade is detected, reduce by -0.3 (the fade belongs to steel_crane's methodology, not this thesis).

### When Data Is Missing

If VWAP for a specific timeframe is unavailable, that timeframe's confirmation is skipped; max achievable confidence caps at the reduced pillar weight. If ATR(14) is unavailable, use ATR(7) as a fallback with -0.1 uncertainty penalty. If volume data is missing entirely, the volume climax signal defaults to neutral (0.0) and apply -0.1 uncertainty. Always check at least two timeframe VWAPs before entering — a single VWAP reference is insufficient.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Short when price is overextended above VWAP. Long when price is compressed below VWAP.
- Leverage: 3x
- Position size: 10% of account per trade
- Stop loss: 1.5% from entry (tight -- if price doesn't revert soon, the thesis is wrong)
- Take profit: VWAP(1h) level (the mean itself is the target)
- Max hold time: 4 hours; if price hasn't reverted to VWAP in 4 hours, the market structure has shifted

## Known Weaknesses

- In strong trending regimes (trending_bull, trending_bear), price can stay extended past VWAP for days -- this agent bleeds to the trend
- Most correlated to `silver_basin` (funding mean-reversion) -- both fade extremes, but from different signal dimensions
- Gap moves through VWAP without reversion (flash crashes, news events) produce instant SL hits
- In high-volatility regimes (crisis), VWAP bands are too wide to be meaningful -- the agent should reduce size or skip
- Bollinger Band-like strategies are well-known; edge compression is real when many participants trade the same mean-reversion setup

## Assets in Focus

Primary: SOL, ETH, AVAX, LINK (clean VWAP behaviour, consistent mean reversion)
Secondary: BTC (tight VWAP bands, lower edge per trade)
Avoid: PEPE, DOGE, WIF, TRUMP (noisy price action, VWAP less reliable as gravity)
""",
    ),
    (
        "violet_lion",
        """# violet_lion -- Thesis v1: Volatility Regime Trader

## Edge Hypothesis

Volatility compresses and expands in predictable cycles. Periods of artificially compressed volatility (ATR in the bottom 25th percentile, tight Bollinger Band width, low volume) are followed by directional expansion events. Periods of extreme volatility (ATR > 80th percentile, wide spreads, elevated liquidations) are statistically likely to contract -- the emotional overreaction fades. The key insight: these volatility regime transitions create directional trading opportunities that are distinct from trend, mean-reversion, or flow signals.

When vol is compressed, the market is coiling. The direction of the coming breakout is unknown ex-ante, but the edge comes from entering with the breakout once it begins, using microstructure clues (book imbalance, aggressive flow) to pick the side. When vol is already expanded, the edge comes from fading the emotional extreme -- the market has overreacted and a mean-reverting snap-back is probable.

This strategy differs from pure mean reversion (jade_hawk) because it does not trade price deviation from VWAP. It trades the vol regime transition itself. In compression it is a breakout follower; in expansion it is a volatility fade. The regime state determines the mode.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Mode Detection

The regime state (compressed or expanded) is determined by ATR and Bollinger Band percentile thresholds. Detection is continuous: an ATR in the 20-30th percentile is a borderline compression signal (weak), while < 15th percentile is unambiguous (strong). Similarly, ATR > 75th percentile is a moderate expansion while > 85th is strong. The mode decision itself carries confidence: a borderline compression signal produces weaker breakout conviction than an unambiguous one.

### Primary Evidence — Compressed Regime (breakout mode)

- **ATR percentile rank**: ATR(14) in the bottom 25th percentile of its 30-day range. < 15th percentile = strong +0.7, 15-25th percentile = moderate +0.4, 25-35th percentile = weak +0.1. Above 35th percentile the market is not compressed — this mode does not apply.
- **Bollinger Band width**: BB width (20, 2) at or below the 20th percentile of its 30-day range adds +0.5. Width 20-30th percentile adds +0.2. Width above 30th percentile contributes nothing. Sustained narrowing for 5+ candles adds an additional +0.2.
- **Volume confirmation**: Volume below the 14-day median adds +0.3 (confirms lack of participation, not distribution). Volume at or above median reduces by -0.2 (distribution possible rather than coiling).
- **Breakout trigger**: A 15m candle closes above the highest high or below the lowest low of the preceding 10 candles. Clean breakout adds +0.5. Marginal breakout (candle closes at the boundary rather than beyond) adds +0.2. No breakout yet contributes 0.0 — the agent waits until this trigger forms.
- **Microstructure direction confirmation**: Bid/ask imbalance from book data or aggressive flow direction supports the breakout direction adds +0.4. Mixed or opposing microstructure reduces by -0.3.

### Primary Evidence — Expanded Regime (vol fade mode)

- **ATR percentile rank**: ATR(14) > 80th percentile of its 30-day range. > 90th percentile = strong +0.7, 80-90th percentile = moderate +0.5, 70-80th percentile = weak +0.2. Below 70th percentile — this mode does not apply.
- **Extreme candle detection**: A recent 15m candle has exceeded 2x the average candle range. Candle > 3x average range adds +0.5 (climax). 2-3x adds +0.3. No extreme candle detected contributes 0.0.
- **Lack of follow-through**: The next 1-2 candles retrace at least 30% of the extreme candle adds +0.4. Retrace of 15-30% adds +0.2. No retrace (cascade continuation) reduces by -0.5 — the fade is premature.
- **Funding extremity**: Funding rate extreme in the direction of the move adds +0.4 (crowded positioning makes the fade safer). Neutral funding adds +0.1. Opposing funding reduces by -0.3.

### Secondary Evidence (both modes)

- **Liquidation cascade status**: If a liquidation cascade has already occurred, add +0.3 (the cascade peak has passed; fading is safer). No cascade detected adds nothing. Cascade still in progress reduces by -0.4.
- **Candle rejection signal**: Extreme candle closed with a long wick adds +0.2 (rejection at the extreme). No significant wick contributes nothing.
- **OI decline confirmation**: OI dropped during the move adds +0.2 (positions being removed, reducing fuel for continuation). OI flat or rising reduces by -0.2.

### When Data Is Missing

If ATR(14) is unavailable, the entire mode detection system is disabled — do not enter. Use ATR(7) as a fallback only with a -0.2 uncertainty penalty and a note that the percentile calculations will use a shorter window. If Bollinger Band data is unavailable, skip the BB width confirmation and rely on ATR alone with -0.1 uncertainty. If book data for microstructure direction is unavailable in compressed mode, use trade flow data if available (from amber_wolf signals) with -0.1 uncertainty; if neither is available, reduce confidence by -0.3 (direction selection becomes guesswork). In expanded mode, if candle data for the most recent 1-3 candles is missing or incomplete, do not enter — the lack-of-follow-through check is essential for the fade thesis. If funding rate data is unavailable in expanded mode, skip the funding check with no penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

**Compressed regime (breakout):**
- Direction: Direction of the breakout (long above range high, short below range low)
- Leverage: 3x
- Position size: 8% of account per trade (smaller -- breakouts have higher variance)
- Stop loss: 1.5% from entry (tight -- false breakouts are common)
- Take profit: 3.0% from entry (ride the breakout)
- Max hold time: 4 hours

**Expanded regime (fade):**
- Direction: Counter to the extreme move. Long after a sell climax, short after a buying climax.
- Leverage: 3x
- Position size: 8% of account
- Stop loss: 1.5% from entry
- Take profit: 2.0% from entry (fade is a quick scalp, not a hold)
- Max hold time: 2 hours (vol fades resolve fast or they fail)

## Known Weaknesses

- False breakouts in compression are the strategy's primary risk -- tight SLs get hit routinely, and the real move starts after the agent is stopped out
- Fading expanded vol during a genuine cascade continuation (flash crash extending) can compound losses -- requires steel_crane's liquidation data to avoid overlapping
- ATR is inherently lagging: by the time compression is identified, the market may have been coiling for days already, and the breakout may come much later
- Requires microstructure data for direction selection -- dependence on the same signals as gray_finch and amber_wolf, creating correlation in compressed regimes
- Most correlated to `steel_crane` during expanded vol (both fade extremes) and to `amber_wolf` during compressed regimes (both follow flow)
- In range_high_vol regimes with no clear expansion/compression cycle, the agent may flip between modes too frequently

## Assets in Focus

Primary: SOL, ETH, SUI (clean vol cycles, reliable compression/expansion patterns)
Secondary: BTC (lower vol variance, but tighter compression signals)
Avoid: Low-liquidity perps (PEPE, WIF, TRUMP) -- vol is permanently elevated, making compression signals unreliable
""",
    ),
    (
        "crimson_fox",
        """# crimson_fox -- Thesis v1: Session Pattern Arbitrage

## Edge Hypothesis

Crypto perpetuals trade 24/7 across global sessions, but participation, order flow, and directional bias follow predictable daily and weekly patterns. These time-based patterns exist because different sets of participants dominate at different hours: APAC retail and systematic flows in the Asian session, European institutions during London hours, US leveraged players at the NY open. Each transition between these participant cohorts creates exploitable directional drift.

The edge is small per trade but highly reliable and available every single day. This is a compounding strategy, not a home-run strategy. The agent does not predict market direction from price action or volume -- it knows what time it is and what statistically tends to happen at that time.

Trading is restricted to specific session windows where historical edge has been established. Outside these windows the agent waits.

## Session Definitions (all times UTC)

### US Open (12:00--13:30 UTC, Mon--Fri)
The highest-volume 90 minutes of the day. BTC and alts frequently gap or break in the first 30 minutes of US cash equities opening. The edge: trade the direction of the first 15-minute candle with a tight stop. If the first 15m candle is green, go long; if red, go short. The US open directional bias has persistence for 60--90 minutes on ~60% of trading days.

### US Afternoon Reversal (16:00--18:00 UTC, Mon--Fri)
The 'puppet show' period where algo desks and institutional flow push into the close. Statistically significant reversal from the US morning trend. If price moved up during the US session (12:00--16:00), expect a mean-reverting pullback. If price moved down, expect a bounce. This is the highest-Sharpe session window.

### Asian Session Drift (00:00--02:00 UTC, Mon--Fri)
Lower liquidity, wider spreads, but consistent directional drift from systematic APAC flows. The edge: trade the direction of the first 30-minute candle of the Asian session. The drift tends to persist for 1--3 hours and is most reliable when it follows a quiet US session (low volatility carries through).

### Weekly Patterns
- **Monday open (00:00 UTC)**: Weekend gap fills are common in the first 4 hours. If BTC gapped up over the weekend, expect a fill-down; if gapped down, expect a fill-up.
- **Friday afternoon (14:00--18:00 UTC)**: Position squaring before the weekend. Longs are closed, shorts are covered. Creates mean-reverting moves.
- **Funding settlement windows (00:00, 08:00, 16:00 UTC)**: Positioning changes 30--60 minutes before settlement as traders adjust leveraged positions. Mild directional bias in the hour leading into settlement.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Session window alignment**: Being within a defined session window is the foundational gate. Strength contribution depends on how deep into the window the entry occurs: first 15 minutes = strong +0.6, mid-window = moderate +0.4, last 15 minutes = weak +0.1 (decaying edge as the window closes).
- **Direction signal from session rule**: The session's directional rule (first candle direction, trend reversal, gap fill) must be present. A clear unambiguous signal contributes +0.5; a marginal signal (small candle, ambiguous gap fill) contributes +0.2.
- **Volume participation**: Volume at expected session levels adds +0.3. Volume significantly below expected (holiday, thin session) reduces confidence by -0.4. Volume above expected adds +0.1 for confirmation.

### Secondary Evidence (moderate weight)

- **No conflicting macro event**: Clean calendar with no major events in the next 2 hours adds +0.2. A known event within 2 hours reduces confidence by -0.5 (the event overrides session patterns).
- **Market regime alignment**: Session signal aligning with the broader market regime (e.g. trend regime + US Open direction) adds +0.3. Conflicting regime reduces by -0.2.
- **Prior session low volatility**: If the prior session showed low volatility (quiet carry-through increases pattern reliability), add +0.2. High prior volatility reduces conviction by -0.2.
- **Multiple pattern confluence**: When multiple session patterns align simultaneously (e.g. Monday US Open AND weekend gap exists), add +0.3. Single-pattern setups get no confluence bonus.
- **Funding rate support**: Neutral or supporting funding adds +0.1; strongly opposing funding reduces by -0.2.

### When Data Is Missing

Session definitions are time-based and always available; the primary evidence is never missing. Volume data may lag: if volume data is stale by more than 5 minutes, treat volume participation as neutral (0.0 instead of the full contribution). Calendar event lookup failures default to assuming no events (cautious assumption — do not veto on missing data). If the current time falls between sessions (no active window), the agent does not trade regardless of other signals — the thesis has no edge outside session windows.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Per session rule. US Open: direction of first 15m candle. US Reversal: counter to session trend. Asian Drift: direction of first 30m candle. Weekly: gap fill direction.
- Leverage: 2x (lower -- time-based edges are small but consistent)
- Position size: 8% of account per trade
- Stop loss: 1.0% from entry (tight -- time-based edge is invalidated quickly if wrong)
- Take profit: Session exit condition (TP at 1.5% or session end, whichever comes first)
- Max hold time: Until the session window closes (maximum 4 hours for Asian drift, 90 minutes for US Open)

## Entry Rules Summary

| Session | When (UTC) | Entry Rule | Max Hold |
|---------|-----------|------------|----------|
| US Open | Mon--Fri 12:00 | Direction of first 15m candle | 90 min |
| US Reversal | Mon--Fri 16:00 | Counter to US session trend (12:00--16:00) | 2h |
| Asian Drift | Mon--Fri 00:00 | Direction of first 30m candle | 3h |
| Monday Gap | Mon 00:00--04:00 | Counter to weekend gap direction | 4h |
| Friday Squaring | Fri 14:00--18:00 | Counter to week's last 4h direction | 4h |
| Pre-settlement | 07:00--08:00, 15:00--16:00, 23:00--00:00 | Direction of improving funding rate | 1h |

## Known Weaknesses

- Small edge per trade requires many repetitions -- statistical significance takes weeks of trading
- Major macro news (FOMC, CPI, NFP) completely overrides session patterns -- must flat through known events
- DST transitions and holiday weeks shift session boundaries -- reduced reliability during transitions
- Weekend sessions (Saturday--Sunday) do not have reliable patterns -- agent skips them entirely
- Most vulnerable to structural market changes that break historical patterns (e.g., ETF approval changing US open behaviour permanently)
- Lowest Sharpe agent on the desk individually -- value is as a uncorrelated compounding machine alongside the other strategies
- Session boundaries are approximate; exact timing shifts with market structure -- requires periodic re-calibration

## Assets in Focus

Primary: BTC, ETH (most consistent session behaviour across all time windows)
Secondary: SOL (growing session consistency, particularly in US hours)
Avoid: Small-cap perps -- session patterns are unreliable when the asset itself drives the flow rather than macro session dynamics
""",
    ),
]


def seed_desk(conn, config: dict) -> list[str]:
    """Seed all initial agents and deploy their compiled specs.

    Spawns the 10 SEED_AGENTS, spawns sage_turtle (compiled event/unlock
    agent), and deploys hand-compiled spec YAML files into the ``specs``
    table for every compiled agent.

    Parameters
    ----------
    conn
        An initialised SQLite connection.
    config
        The *full* config dict (``config.yaml`` loaded via yaml.safe_load).

    Returns
    -------
    list[str]
        Agent IDs that were seeded.
    """
    desk_config = config.get("desk", {})
    starting_balance = float(desk_config.get("starting_balance", 50000.0))
    created: list[str] = []

    for name, thesis in SEED_AGENTS:
        spawn_agent(
            conn,
            name,
            thesis,
            status="rookie",
            config_overrides=CONFIG_OVERRIDES.get(name),
            starting_balance=starting_balance,
        )
        created.append(name)

    spawn_agent(
        conn,
        "sage_turtle",
        _SAGE_TURTLE_THESIS,
        status="rookie",
        config_overrides={"compiled": True, "wake_interval": 300},
        starting_balance=starting_balance,
    )
    created.append("sage_turtle")

    for name in _COMPILED_AGENTS:
        spec_path = SPECS_DIR / f"{name}_v1.yaml"
        if not spec_path.exists():
            continue
        spec = load_spec(str(spec_path))
        deploy_spec(conn, name, spec, config=desk_config)

    return created


def main():
    print("=" * 60)
    print("FORGE -- Fresh Start")
    print("=" * 60)
    print()
    print("This will DELETE ALL EXISTING DATA and seed 10 new agents.")
    print()

    if len(sys.argv) > 1 and sys.argv[1] == "--yes":
        confirmed = True
    else:
        resp = input("Are you sure? (y/N): ").strip().lower()
        confirmed = resp in ("y", "yes")

    if not confirmed:
        print("Cancelled.")
        return

    # Wipe existing DB (including WAL/SHM sidecar files from journal_mode=WAL)
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"Deleted existing database: {DB_PATH}")
    else:
        print("No existing database found.")

    for suffix in ("-wal", "-shm"):
        sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
            print(f"Deleted stale sidecar file: {sidecar}")

    # Retire legacy pre-ledger capture -- superseded by ledger/ (see
    # docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md).
    # Wrong schema for the new system either way, so nothing here is worth
    # migrating forward.
    legacy_historical_dir = PROJECT_ROOT / "data" / "historical_data"
    if legacy_historical_dir.exists():
        shutil.rmtree(legacy_historical_dir)
        print(f"Deleted legacy historical capture: {legacy_historical_dir}")

    oi_history_path = PROJECT_ROOT / "data" / "heartbeat_oi_history.json"
    if oi_history_path.exists():
        oi_history_path.unlink()
        print(f"Deleted stale OI history baseline: {oi_history_path}")

    # Initialize fresh schema
    conn = get_connection(str(DB_PATH))
    init_schema(conn)

    full_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    created = seed_desk(conn, full_config)
    starting_balance = float(
        full_config.get("desk", {}).get("starting_balance", 50000.0)
    )
    for name in created:
        print(
            f"  Created agent: {name} (status=rookie, balance=${starting_balance:,.0f})"
        )

    conn.close()

    # Verify
    conn = get_connection(str(DB_PATH))
    count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    bal_count = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE mode='paper'"
    ).fetchone()[0]
    conn.close()

    print()
    print(f"Done. {count} agents seeded, {bal_count} account snapshots created.")
    print()
    print("Ready. Run: python forge.py")


if __name__ == "__main__":
    main()
