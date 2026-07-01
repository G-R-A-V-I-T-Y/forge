# jade_hawk -- Thesis v1: Regime Detection

## Edge Hypothesis

Market regime is the single most important conditioning variable for every trading strategy. A momentum strategy that thrives in trending markets destroys capital in ranging markets. A mean-reversion strategy that prints in range markets bleeds in trends. The most valuable agent on the desk is not the one that trades best -- it is the one that correctly identifies what kind of market we are in right now.

jade_hawk does not trade. It never recommends entry, exit, or position sizing directly. Its sole output is a regime classification tag that every other agent on the desk receives as part of their decision context. The classification uses inputs from across the market: BTC price action (trend direction and strength, measured by ADX and moving average slope), cross-asset volatility (ATR percentiles across the universe), funding rate distribution (are rates extreme
across the board or neutral?), OI trends (aggregate OI rising or falling across the desk?), liquidation activity (elevated or normal?), and correlation structure (are assets moving together or rotating?).
The output is one of nine regime tags, assigned deterministically with a confidence score, and the reasoning that led to the classification.

## Regime Taxonomy

1. **Trending** -- Strong directional move with ADX > 25 and price
   making higher highs / lower lows consistently. Sub-classified as
   bullish or bearish.
2. **Mean-reverting** -- Range-bound price with no clear direction;
   ATR is average or slightly above; price oscillates between
   established support and resistance.
3. **High volatility** -- ATR > 90th percentile of 14-day range;
   wide spreads, elevated liquidation activity; price gaps common.
4. **Low volatility** -- ATR < 20th percentile of 14-day range;
   tight ranges, low volume, compressed spreads.
5. **Risk-on** -- BTC and alts rising together; funding rates positive
   across the board; OI increasing; correlation matrix converging to
   high values (everything moves together).
6. **Risk-off** -- BTC falling; alts falling more; funding across
   assets negative (short premium); OI declining; correlation
   spiking (fear-driven synchronous selling).
7. **Funding frenzy** -- Funding rates are extreme across 5+ assets
   simultaneously; suggests market-wide positioning extreme.
8. **Panic** -- Liquidations cascading across multiple assets;
   OI dropping sharply; spreads blowing out; VRP-like regime in
   perps; this is a crisis classification.
9. **Quiet accumulation** -- Low volatility, neutral funding, stable
   OI, correlation matrix normalising; price drifting sideways to
   slightly up; this is the 'nobody cares' regime where smart money
   positions.

## Classification Method

**Deterministic, not LLM-based.** The classifier is a decision tree with threshold-based rules derived from the input data. The output is a single regime tag and a confidence score (0.0-1.0) based on how many signals point to the same classification. When no single regime has high confidence (> 0.6), the tag is 'mixed' and the confidence scores for all regimes are passed alongside so consuming agents can
weight their decisions accordingly.

**Required inputs for each classification cycle:**
- BTC 4h OHLCV (last 30 candles)
- ATR percentiles for BTC, ETH, SOL (14-day window)
- Funding rates for all 15 universe assets
- Aggregate OI change (across all assets, 24h)
- Total liquidation volume (last 4 hours)
- Average cross-asset correlation (20-period rolling)

## Output Schema

A regime report is written as a structured JSON object to the database and made available to all agents at decision time:

```json
{
  "regime": "trending_bull",
  "confidence": 0.82,
  "secondary": {"mean_reverting": 0.12, "high_vol": 0.06},
  "reasoning": "BTC ADX at 32 with 4 consecutive 4h higher highs. "
    "Funding neutral. OI rising 6% across the desk. "
    "No elevated liquidation activity. Correlation at 0.75.",
  "classifier_version": 1
}
```

## Interaction with Other Agents

- Every other agent receives the regime tag and confidence in their
  decision prompt
- Agents are expected to condition their confidence on the regime:
  a momentum agent should reduce size when regime is mean_reverting
- The meta-agent (`crimson_fox`) uses jade_hawk's output as its
  primary conditioning variable for confidence multipliers
- When confidence is low (< 0.6), agents are told to use their own
  best judgment and note the regime ambiguity

## Known Weaknesses

- Regime changes at inflection points are inherently lagging
  (the classifier needs confirming candles)
- During rapid regime transitions (e.g. panic from quiet),
  the classifier can miss the transition until it is well underway
- BTC-centric classification may miss altcoin-local regimes
- Requires high-quality data across all 15 assets to be accurate
- A deterministic classifier cannot adapt to novel regime types
  without manual version updates
