# violet_lion -- Thesis v1: Volatility Regime Trader

## Edge Hypothesis

Volatility compresses and expands in predictable cycles. Periods of artificially compressed volatility (ATR in the bottom 25th percentile, tight Bollinger Band width, low volume) are followed by directional expansion events. Periods of extreme volatility (ATR > 80th percentile, wide spreads, elevated liquidations) are statistically likely to contract -- the emotional overreaction fades. The key insight: these volatility regime transitions create directional trading opportunities that are distinct from trend, mean-reversion, or flow signals.

When vol is compressed, the market is coiling. The direction of the coming breakout is unknown ex-ante, but the edge comes from entering with the breakout once it begins, using microstructure clues (book imbalance, aggressive flow) to pick the side. When vol is already expanded, the edge comes from fading the emotional extreme -- the market has overreacted and a mean-reverting snap-back is probable.

This strategy differs from pure mean reversion (jade_hawk) because it does not trade price deviation from VWAP. It trades the vol regime transition itself. In compression it is a breakout follower; in expansion it is a volatility fade. The regime state determines the mode.

## Entry Conditions

**Compressed regime (coil -- anticipate breakout):**

**Required:**
1. ATR(14) on the target asset is in the bottom 25th percentile of its 30-day range
2. Bollinger Band width (20, 2) is at or below the 20th percentile of its 30-day range (bands are squeezing)
3. Volume is below the 14-day median (confirms lack of participation, not distribution)
4. Price breaks beyond the tight range: a 15m candle closes above the highest high or below the lowest low of the preceding 10 candles
5. Microstructure confirmation: bid/ask imbalance (from book data) or aggressive flow direction (from trade flow) supports the breakout direction

**Supporting (raise confidence, not required):**
- Volume on the breakout candle > 1.5x average 15m volume
- Funding is neutral (no artificial positioning pressure)
- The breakout direction aligns with the dominant regime (trending regime + upside vol breakout = strong confluence)
- Narrowing of Bollinger Bands has been sustained for 5+ candles

**Expanded regime (vol fade -- fade the extreme):**

**Required:**
1. ATR(14) > 80th percentile of its 30-day range
2. A recent 15m candle has exceeded 2x the average candle range (extreme candle)
3. The move lacks follow-through: the next 1-2 candles retrace at least 30% of the extreme candle
4. Funding rate is extreme in the direction of the move (crowded positioning -- fade is safer)

**Supporting (raise confidence, not required):**
- Liquidation cascade has already occurred (steel_crane would have entered; this agent joins the fade after the cascade peak)
- The extreme candle closed with a long wick (rejection at the extreme)
- OI dropped during the move (positions being liquidated, reducing fuel for continuation)

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
