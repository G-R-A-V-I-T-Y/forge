# copper_vane -- Thesis v1: Open Interest Intelligence

## Edge Hypothesis

Open interest reveals conviction behind price moves better than price alone. The OI×Price regime matrix classifies market states into four categories, each with a clear directional implication:

- Rising price + rising OI = genuine new participation. Capital is
  entering the asset, longs and shorts are both adding. This is
  a healthy trend -- go with it.
- Rising price + falling OI = short squeeze or distribution. Price
  is rising but total exposure is shrinking -- shorts are being
  forced out or smart money is distributing. Fade the move.
- Falling price + rising OI = aggressive new shorts. Capital is
  entering on the short side, indicating informed selling. Join it.
- Falling price + falling OI = longs giving up. Capitulation without
  new short conviction -- wait for reversal.

OI data from Hyperliquid updates every few seconds and reflects real on-chain positioning. This signal is difficult to spoof and leads price, especially at turning points. The agent ignores raw price action as a primary signal and classifies every 4-hour window into one of the four OI×Price quadrants, entering only when the classification is unambiguous.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **OI×Price regime classification**: A clear, sustained classification over the full 4-hour window contributes +0.5. Ambiguous regimes (flipping between quadrants) reduce confidence by -0.3; the regime must be resolved before entry is viable.
- **OI change magnitude**: Stronger signals at larger values (>3% = strong +0.8, 1.5-3% = moderate +0.5, <1% = weak +0.2). Direction of change determines sign: rising OI is positive for rising price setups, negative for falling price setups.
- **Price change magnitude**: Stronger moves carry more conviction (>1.5% = strong +0.6, 0.5-1.5% = moderate +0.3, <0.5% = weak +0.1). Sign must align with OI direction for the classification to hold.
- **Prior window consistency**: If the prior 4-hour window shows a conflicting OI regime, reduce confidence by 30-40%; if consistent, maintain full conviction.

### Secondary Evidence (moderate weight)

- **Volume confirmation**: Volume direction matching OI direction adds +0.3; opposing direction reduces by -0.2.
- **Funding rate support**: Funding neutral or supporting OI thesis adds +0.2; strongly opposing funding reduces confidence by -0.3.
- **Sector-wide OI change**: Broad OI movement across the sector adds +0.2 to conviction; isolated OI in a single asset adds nothing.

### When Data Is Missing

If OI data is unavailable, the regime classification and OI change pillars are removed: maximum achievable confidence drops to ~40%. No entry is warranted in this state — the thesis has no edge without OI data. If price data is available but lagging, use 1-minute snapshots instead of real-time; treat staleness as a -0.2 uncertainty penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: Determined by OI×Price quadrant classification.
   Rising+Rising = Long. Rising+Falling = Short. Falling+Rising =
   Short. Falling+Falling = Wait (no entry).
- Leverage: 3x
- Position size: 12% of account per trade
- Stop loss: 2.0% from entry
- Take profit: 4.0% from entry (2:1 reward/risk)
- Max hold time: 8 hours

## Known Weaknesses

- In low-volume regimes, OI changes are small and classification
  becomes unreliable
- OI data can lag during high-volatility events on Hyperliquid
- After large liquidation cascades, OI drops mechanically -- this
  creates false 'falling+falling' signals
- Most effective on mid-cap perps (SOL, ARB, SUI) where OI
  changes are driven by conviction, not passive hedging

## Assets in Focus

Primary: SOL, ETH, SUI, ARB, OP (good OI depth)
Secondary: BTC, BNB (deep OI but lower predictive variance)
Avoid: Assets with <$10M OI (noise-dominated)
