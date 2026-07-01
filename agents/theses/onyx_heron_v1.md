# onyx_heron -- Thesis v1: Relative Value

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
