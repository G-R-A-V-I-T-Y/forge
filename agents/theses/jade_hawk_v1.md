# jade_hawk -- Thesis v1: VWAP Mean Reversion

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
