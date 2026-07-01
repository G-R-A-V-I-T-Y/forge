# amber_wolf -- Thesis v1: Trade Flow

## Edge Hypothesis

Every market transaction carries information. Aggressive buyers (market buys lifting the ask) and aggressive sellers (market sells hitting the bid) reveal the direction of informed flow. By analysing every execution in real-time, the agent can detect when institutional or otherwise informed capital is moving directionally before that flow is fully reflected in the mid-price.

Key metrics: aggressive buy volume vs aggressive sell volume over rolling windows (1m, 5m, 15m), VWAP relative to the mid-price (informed flow pays the spread -- elevated VWAP above mid confirms buying pressure), buy pressure ratio (agg buys / total agg volume), average trade size (block trades vs retail -- a surge in average size signals institutional participation), and cumulative delta (agg buys minus agg sells over a window). The signature of informed flow is overwhelming one-sided aggression at above-average trade size, sustained across multiple time windows.

## Entry Conditions

**Required (all must be met):**
1. Buy pressure ratio > 0.65 (buy-heavy) or < 0.35 (sell-heavy)
   across the 5-minute window
2. Average trade size > 1.5x the 1-hour rolling average (confirms
   institutional-sized participation, not retail noise)
3. Cumulative delta is directionally consistent across all three
   windows (1m, 5m, 15m) -- no divergence pattern
4. VWAP deviation from mid-price > 0.05% in the direction of flow
   (aggressors are willing to pay up, confirming conviction)

**Supporting (raise confidence, not required):**
- Funding rate supports the flow direction (flow + funding = high
  conviction)
- Order book imbalance (from `gray_finch` style micro reading)
  agrees with flow direction
- Volume is accelerating into the move (1m > 5m > 15m on a
  per-minute normalised basis)

## Position Parameters

- Direction: Direction of aggressive flow
- Leverage: 3x
- Position size: 10% of account per trade
- Stop loss: 1.5% from entry
- Take profit: 3.0% from entry (2:1 reward/risk)
- Max hold time: 30 minutes -- flow signals decay; if the move
  hasn't materialised in 30m, the flow was likely absorption, not
  accumulation

## Known Weaknesses

- In heavily algorithmic markets (BTC), trade flow can be
  strategically spoofed or split across multiple venues
- During low-volume periods, average trade size is unreliable
- Flow analysis works best on mid-cap perps where institutional
  flow is concentrated enough to measure
- Correlated to `gray_finch` during high-volume regimes -- both
  read the same book from different angles

## Assets in Focus

Primary: SOL, SUI, ARB, OP (concentrated institutional flow)
Secondary: BTC, ETH (deep flow but higher signal-to-noise ratio
  required)
Avoid: Meme coins (PEPE, WIF, DOGE) -- retail-dominated flow
