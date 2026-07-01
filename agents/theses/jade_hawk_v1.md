# jade_hawk -- Thesis v1: VWAP Mean Reversion

## Edge Hypothesis

Price consistently overshoots and reverts to VWAP across multiple timeframes, especially at statistical extremes. VWAP acts as a gravitational centre for price: institutions execute large orders algorithmically around VWAP, market-makers hedge around it, and the settlement mechanics of perps create natural reversion pressure. When price deviates significantly from VWAP -- more than two ATRs away on a 15-minute chart -- the probability of reversion within the next 1-4 hours increases well above random.

This agent fades price extremes relative to VWAP. It does not predict direction -- it predicts that price is statistically stretched and will revert toward the mean. Every momentum agent on the desk buys strength; jade_hawk sells it and buys weakness. This negative correlation to the desk's trend strategies provides natural diversification.

The signal is strongest when multiple timeframe VWAPs (15m, 1h, 4h) all show deviation in the same direction, confirming that the move is extended on multiple execution horizons. Volume confirmation distinguishes a climax (reversion likely) from a genuine breakout (momentum likely).

## Entry Conditions

**Required (all must be met):**

**For short entries (price overextended above VWAP):**
1. Current price > VWAP(1h) + 2.0 * ATR(14) on the 15-minute chart
2. VWAP(4h) is also below current price (multi-timeframe confirmation)
3. Volume on the move is > 1.5x the 14-period average volume on the 15m chart (climax volume, not quiet drift)
4. The move has been sustained for at least 3 consecutive 15m candles (not a single wick)

**For long entries (price compressed below VWAP):**
1. Current price < VWAP(1h) - 2.0 * ATR(14) on the 15-minute chart
2. VWAP(4h) is also above current price (multi-timeframe confirmation)
3. Volume on the move is > 1.5x the 14-period average volume
4. The move has been sustained for at least 3 consecutive 15m candles

**Supporting (raise confidence, not required):**
- RSI(14) on 1h chart > 70 (short) or < 30 (long) -- classic overbought/oversold
- Funding rate supports the fade direction (extreme positive funding + price above VWAP = strong short signal)
- The regime is range_low_vol or range_high_vol (mean-reversion environments)
- No major scheduled event for the asset in the next 4 hours
- The move is not driven by a liquidation cascade (would favour steel_crane for the fade, not this strategy)

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
