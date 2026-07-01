# onyx_heron -- Thesis v1: Relative Value

## Edge Hypothesis

Within the crypto perpetuals universe, assets in the same sector or with structural relationships (SOL vs ETH, BTC vs ETH, AI-token basket vs Layer-1 basket) diverge and converge in statistically predictable patterns. These spreads are driven by temporary capital flows, narrative shifts, and leverage dynamics that mean-revert over hours to days. Trading the spread removes the need to predict absolute market direction -- the edge comes from correctly identifying when one asset is statistically cheap or rich relative to another.

The agent monitors a fixed set of pairs and baskets using z-score of the spread vs a rolling window, rolling correlation (assets must remain structurally related for the thesis to hold), and cointegration tests (pairs that drift apart must tend to revert, not diverge permanently). Entry is triggered when a spread is extreme (z-score > 2.0 or < -2.0) and cointegration is confirmed. The agent is always long the cheap leg and short the rich leg, reducing market beta to near zero when sized correctly.

## Entry Conditions

**Required (all must be met):**
1. Spread z-score > 2.0 (asset A is expensive vs B) or < -2.0
   (asset A is cheap vs B) based on the 7-day rolling window
2. Rolling 20-period correlation between the two assets > 0.60
   (the relationship still holds at the structural level)
3. Cointegration test p-value < 0.05 (the spread is mean-reverting,
   not a random walk divergence)
4. The spread extreme is not driven by a known fundamental catalyst
   on one leg (e.g. SOL network outage, ETH ETF news) -- these are
   regime changes, not mean-reversion setups

**Supporting (raise confidence, not required):**
- Funding rates on both legs are neutral (no artificial pressure)
- The regime tag is range_low_vol or range_high_vol (mean-reversion
  environments favour this thesis)
- Other pairs in the same sector show normal spreads (idiosyncratic)
- Spread has been extreme for 2+ hours (persistence confirms the
  regime hasn't changed)

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
