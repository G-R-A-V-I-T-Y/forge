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

## Entry Conditions

**Required (all must be met):**
1. Clear OI×Price regime classification confirmed for the current
   4-hour window with no ambiguity (direction consistency in both
   axes over the full window)
2. OI change magnitude > 3% in the classification direction
   (confirms the signal is meaningful, not noise)
3. Price change magnitude > 1.5% (confirms the move has market
   impact)
4. No conflicting OI regime in the prior 4-hour window
   (reversal signals require two consecutive windows to confirm)

**Supporting (raise confidence, not required):**
- Volume confirms: volume direction matches OI direction
- Funding rate is neutral or supports the OI thesis
- The OI change is broad across the sector (not just one asset)

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
