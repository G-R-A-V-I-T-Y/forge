# jade_hawk — Thesis v1: Funding Rate Mean Reversion

## Edge Hypothesis

Crypto perpetual markets generate funding rates that periodically diverge far from
equilibrium due to one-sided speculative positioning. When funding rates stay persistently
negative (longs pay shorts, meaning market is net short), mechanical squeeze pressure builds:
shorts pay funding each 8-hour period, reducing their risk-adjusted return. Once that cost
becomes sufficiently high, short covering accelerates, driving price up even without a
fundamental catalyst.

**Primary signal:** Funding rate negative for 3+ consecutive 8-hour periods on an asset
in my universe, with current rate ≤ -0.03%.

## Entry Conditions

**Required (all must be met):**
1. Funding rate ≤ -0.03% for current period
2. Funding rate was also negative in at least 2 of the prior 3 periods (persistence check)
3. Price has not already rallied >3% in the last 4h (squeeze not already underway)
4. Open interest has not fallen >10% in 24h (capitulation already complete = no squeeze fuel)

**Supporting (raise confidence, not required):**
- Recent long liquidation volume > $5M/h (trapped longs being cleaned out = cleaner setup)
- BTC dominance stable or falling (risk-on environment favors the trade)
- Asset is near a 15m support level

## Position Parameters

- Direction: Long always (fade the short squeeze)
- Leverage: 3x (low leverage for squeeze trades — timing is imprecise)
- Position size: 10% of account per trade
- Stop loss: 2.0% below entry price
- Take profit: 4.5% above entry price (2.25:1 reward/risk)
- Max hold time: 8 hours (if TP not hit, evaluate at funding reset)

## Known Weaknesses

- In persistent trending bear markets, negative funding can remain negative for weeks
  without triggering a squeeze — this thesis underperforms in `trending_bear` regime
- Works best in `range_high_vol` and `trending_bull` regimes
- Timing risk: squeeze can take 2-12 hours to materialize; overnight gaps can hit SL

## Assets in Focus

Primary: SOL, ETH, ARB, OP (mid-cap perps with meaningful funding volatility)
Secondary: BTC (lower funding variance but high liquidity)
Avoid: PEPE, WIF, DOGE (too noisy, funding spikes don't mean squeeze)
