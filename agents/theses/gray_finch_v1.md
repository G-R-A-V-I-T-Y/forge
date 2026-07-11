# gray_finch -- Thesis v1: Order Book Microstructure

## Edge Hypothesis

Over short time horizons (5-20 minutes), order book microstructure predicts imminent price direction more reliably than any lagging indicator based on OHLCV data. The book shows exactly where supply and demand sit, who is willing to pay what, and where the market will encounter resistance or support in the next few ticks.

This agent computes bid/ask imbalance (the ratio of total bid size to total ask size across the top 10 levels), identifies liquidity gaps between price levels (zones with thin book depth where price can move quickly), detects resting walls (clusters of large limit orders that act as magnets or resistance), tracks spread width relative to its recent history, and estimates expected slippage for a position of the agent's target size. Queue dynamics measure whether the inside bid or ask is being eaten through or replenished. The agent never looks at 24h charts, funding rates, or any macro-level data -- the microstructure is the complete picture at the decision horizon.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Directional imbalance**: Bid/ask ratio across the top 10 levels. For long entries: ratio > 1.5 = strong +0.7, 1.2-1.5 = moderate +0.4, 1.0-1.2 = weak +0.1. For short entries: ratio < 0.67 = strong +0.7, 0.67-0.85 = moderate +0.4, 0.85-1.0 = weak +0.1. Ratios near 1.0 contribute near-zero directional conviction.
- **Resting wall proximity**: No resting wall within 0.5% of current price in the entry direction is ideal (+0.0 penalty). A wall within 0.5% reduces confidence by -0.5; a wall within 0.25% is a near-certain stopper, reducing by -0.8. If the wall is on the opposite side of the book, it may act as support — add +0.2.
- **Liquidity gap existence**: A liquidity gap within 1% of current price in the entry direction adds +0.5. A gap within 0.5% adds +0.7 (clean path for price movement). No gap within 1% reduces confidence by -0.3 (price will fight through stacked book depth).

### Secondary Evidence (moderate weight)

- **Expected slippage**: Slippage estimate for target size. Slippage < 0.05% adds +0.3; 0.05-0.1% adds +0.1; 0.1-0.2% reduces by -0.2; > 0.2% reduces by -0.5 (cost destroys edge).
- **Inside queue dynamics**: Queue being consumed aggressively (not replenished) in the entry direction adds +0.3. Queue being replenished faster than consumed reduces by -0.2. Neutral queue gets 0.0.
- **Spread tightness**: Spread tighter than the 20-period moving average adds +0.2 (active liquid market). Wider than average reduces by -0.2 (stale or gappy book).
- **Recent trade direction**: Small trades (1-5 contracts) predominantly on the aggressive side of the entry direction adds +0.2. Mixed or opposing trade flow reduces by -0.1.

### When Data Is Missing

Order book data is the foundation of this thesis — if the top 10 levels of the book are unavailable or stale by more than 1 second, do not enter. The microstructure signal decays too quickly to use cached data. Partial book data (e.g. only top 5 levels) reduces the effective imbalance calculation: treat the available levels as the full picture but apply a -0.2 uncertainty penalty. If slippage estimation is unavailable, assume the midpoint of the range (0.1%) and apply a -0.1 uncertainty penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

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
