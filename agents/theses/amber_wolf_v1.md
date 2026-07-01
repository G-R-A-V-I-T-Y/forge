# amber_wolf -- Thesis v1: Trade Flow

## Edge Hypothesis

Every market transaction carries information. Aggressive buyers (market buys lifting the ask) and aggressive sellers (market sells hitting the bid) reveal the direction of informed flow. By analysing every execution in real-time, the agent can detect when institutional or otherwise informed capital is moving directionally before that flow is fully reflected in the mid-price.

Key metrics: aggressive buy volume vs aggressive sell volume over rolling windows (1m, 5m, 15m), VWAP relative to the mid-price (informed flow pays the spread -- elevated VWAP above mid confirms buying pressure), buy pressure ratio (agg buys / total agg volume), average trade size (block trades vs retail -- a surge in average size signals institutional participation), and cumulative delta (agg buys minus agg sells over a window). The signature of informed flow is overwhelming one-sided aggression at above-average trade size, sustained across multiple time windows.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Buy pressure ratio**: Aggressive buy volume divided by total aggressive volume across the 5-minute window. For long entries: ratio > 0.70 = strong +0.7, 0.60-0.70 = moderate +0.5, 0.55-0.60 = weak +0.2. For short entries: ratio < 0.30 = strong +0.7, 0.30-0.40 = moderate +0.5, 0.40-0.45 = weak +0.2. Ratios near 0.50 contribute near-zero directional conviction.
- **Average trade size anomaly**: Average trade size relative to the 1-hour rolling average. > 2.0x = strong +0.6 (clear institutional participation), 1.5-2.0x = moderate +0.4, 1.2-1.5x = weak +0.1. Below 1.2x the participant profile is retail noise — contribute 0.0.
- **Cumulative delta consistency**: Cumulative delta directionally consistent across all three windows (1m, 5m, 15m) adds +0.5. Two of three windows aligned adds +0.3. One window aligned (divergent otherwise) adds +0.1. No alignment (accumulation-distribution pattern, sawtooth delta) reduces by -0.4 — this is absorption, not accumulation.

### Secondary Evidence (moderate weight)

- **VWAP deviation from mid**: Deviation > 0.10% in the direction of flow adds +0.3 (aggressors paying up confirms conviction). Deviation 0.05-0.10% adds +0.1. Below 0.05% contributes nothing. Deviation opposing flow direction reduces by -0.3.
- **Funding rate support**: Funding rate supporting the flow direction adds +0.2 (flow + funding converging = high conviction). Opposing funding reduces by -0.2. Neutral funding adds nothing.
- **Order book imbalance**: Book imbalance (from gray_finch style micro reading) agreeing with flow direction adds +0.2. Book disagreeing reduces by -0.2. Book data unavailable: skip with no penalty.
- **Volume acceleration**: Volume accelerating into the move (1m > 5m > 15m on a per-minute normalised basis) adds +0.3. Flat or decelerating volume profile adds 0.0.

### When Data Is Missing

If trade flow data for the 5-minute window is unavailable (missed execution feed ticks), do not enter — flow is the sole foundation of this thesis and stale data is worthless. If the 1-hour rolling average for trade size is unavailable (not enough history since agent start), use the available shorter history with a -0.1 uncertainty penalty per missing hour below 1 hour (maximum -0.3). If VWAP data is unavailable, skip the VWAP deviation check with no penalty. If book imbalance data is unavailable, skip with no penalty. If funding rate data is unavailable, skip with no penalty. If cumulative delta can only be computed on a subset of the windows (e.g. only 5m and 15m available), treat as two-of-three alignment with the available windows.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

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
