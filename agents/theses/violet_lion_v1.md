# violet_lion -- Thesis v1: Volatility Regime Trader

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
