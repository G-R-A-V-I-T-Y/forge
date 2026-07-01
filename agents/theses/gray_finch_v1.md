# gray_finch -- Thesis v1: Order Book Microstructure

## Edge Hypothesis

Over short time horizons (5-20 minutes), order book microstructure predicts imminent price direction more reliably than any lagging indicator based on OHLCV data. The book shows exactly where supply and demand sit, who is willing to pay what, and where the market will encounter resistance or support in the next few ticks.

This agent computes bid/ask imbalance (the ratio of total bid size to total ask size across the top 10 levels), identifies liquidity gaps between price levels (zones with thin book depth where price can move quickly), detects resting walls (clusters of large limit orders that act as magnets or resistance), tracks spread width relative to its recent history, and estimates expected slippage for a position of the agent's target size. Queue dynamics measure whether the inside bid or ask is being eaten through or replenished. The agent never looks at 24h charts, funding rates, or any macro-level data -- the microstructure is the complete picture at the decision horizon.

## Entry Conditions

**Required (all must be met):**
1. Directional imbalance: bid/ask ratio > 1.5 (for longs) or
   < 0.67 (for shorts) across the top 10 levels
2. No resting wall within 0.5% of current price in the entry
   direction (a large limit order cluster that would stop the move)
3. Liquidity gap exists within 1% of current price in the entry
   direction (thin book allowing clean price movement)
4. Expected slippage for target size is < 0.1% of notional

**Supporting (raise confidence, not required):**
- Inside queue is being consumed, not replenished (aggressive flow
  eating into the passive side)
- Spread is tighter than the 20-period moving average (active market)
- Recent small trades (1-5 contracts) are predominantly on the
  aggressive side of the direction

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
