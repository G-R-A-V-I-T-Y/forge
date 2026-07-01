# violet_lion -- Thesis v1: Volatility Trader

## Edge Hypothesis

Volatility is a distinct asset class in crypto perpetuals that behaves independently of direction. Periods of artificially compressed volatility (ATR in the bottom 20th percentile, tight ranges, low volume) are followed by volatility expansion events that create opportunities regardless of which direction the price breaks. Conversely, periods of extreme volatility (ATR > 90th percentile) are statistically likely to contract, reducing the probability of large continuations.

violet_lion does not take directional trades. It never predicts whether price will go up or down. Instead, it predicts the magnitude of future price movement and adjusts position sizing across the desk accordingly. When volatility is artificially compressed, the agent increases position size recommendations (the market is coiling and due for expansion). When volatility is extreme, it decreases position sizes (the market is unstable and directional edge is harder to find). violet_lion acts as a vol overlay on the desk, outputting a position-size multiplier that other agents reference.

## Entry Conditions (Volatility State Classification)

**Compressed regime (increase sizing):**
1. ATR(14) across BTC, ETH, SOL is in the bottom 20th percentile
   of its 60-day range
2. Range compression: last 20 candles on 4h timeframe show a range
   narrower than any 20-candle window in the last 60 days
3. Average daily true range declining for 5+ consecutive days
4. Volume declining or flat at compressed levels (confirms lack of
   participation, not distribution)

**Expanded regime (decrease sizing):**
1. ATR(14) > 90th percentile of 60-day range
2. Recent large candles (> 2x average range) without follow-through
   (range expansion without trend -- high noise)
3. Spread widening across the book (market-maker risk premium
   increasing)
4. Elevated liquidation activity (cascading liquidations = vol
  begets vol)

## Output

violet_lion writes a vol state report to the database containing:
- Vol regime: compressed | normal | expanded | crisis
- Position size multiplier: 1.5x (compressed), 1.0x (normal),
  0.5x (expanded), 0.25x (crisis)
- Confidence score (0.0-1.0)
- ATR percentiles for each tracked asset
- Expected vol expansion direction if compressed (no signal if
  uncertainty is high)

The multiplier is advisory -- each agent decides whether and how
to apply it. `crimson_fox` (meta agent) incorporates vol state
into its confidence multiplier calculations.

## Position Parameters

- Direction: None (non-directional vol overlay)
- Leverage: N/A
- Position size: N/A -- outputs a multiplier (0.25x to 1.5x)
- Stop loss: N/A
- Take profit: N/A

## Known Weaknesses

- Volatility compression can persist far longer than expected
  (the 'coiled spring that never springs')
- During crisis events, volatility explodes and the 0.25x
  multiplier may still be too aggressive
- ATR-based vol measures are inherently lagging -- by the time
  expansion is confirmed, the best entry may have passed
- Overlap with jade_hawk's regime classifier: high volatility and
  panic regimes often coincide

## Assets in Focus

All 15 universe assets -- vol state is desk-wide, not per-asset
