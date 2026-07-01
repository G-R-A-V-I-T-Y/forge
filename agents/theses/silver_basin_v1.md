# silver_basin -- Thesis v1: Funding Dislocation

## Edge Hypothesis

Funding rates are the purest expression of leverage demand in the crypto perpetuals market. When traders are willing to pay extreme premiums (positive funding) or receive extreme discounts (negative funding), they are expressing a conviction that is statistically likely to be wrong at the extreme. Funding dislocations are self-correcting: the cost of holding a trade mechanically reduces the edge of the crowded side until it unwinds.

This agent studies nothing but funding. It computes the current funding rate z-score vs a rolling 14-day history, the funding trend (direction of the last 3 periods), predicted funding from OI changes (when OI surges, funding tends to follow), and funding acceleration (the rate of change of funding itself). Entry is triggered when funding is statistically irrational -- extreme z-scores combined with accelerating trend -- and held until funding normalises. Price action is deliberately excluded to avoid confirmation bias.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Funding rate extremity (z-score)**: The distance of the current funding rate from its 14-day mean, measured in standard deviations. z-score > 2.0 or < -2.0 = strong +0.7, 1.5-2.0 = moderate +0.5, 1.0-1.5 = weak +0.2. Below 1.0 the funding rate is within normal range — contribute 0.0 for dislocation signals. The sign of the z-score determines direction: positive extreme = short signal, negative extreme = long signal.
- **Funding acceleration**: The rate of change of funding in the direction of the dislocation. Acceleration in the last period matching the dislocation direction adds +0.5. Flat acceleration (last period funding is similar to prior) adds +0.2. Deceleration (funding starting to revert) reduces confidence by -0.4 — the best entry may have passed.
- **OI-funding alignment**: OI change supporting the funding thesis. For positive funding extreme: OI flat or falling (late-stage crowding) adds +0.3; OI still rising (still building) adds +0.1 but warns the dislocation may have further to run. For negative funding extreme: OI stable or rising (building shorts) adds +0.3; OI falling adds +0.1. OI data unavailable: treat as neutral but apply -0.1 uncertainty penalty.

### Secondary Evidence (moderate weight)

- **Persistence of extreme**: Funding has been extreme for 2+ consecutive funding periods adds +0.3 (confirms the dislocation is structural, not a one-period anomaly). Single-period extreme contributes nothing.
- **Predicted funding agreement**: The predicted funding from the OI model agreeing with the current extreme adds +0.2. Disagreement reduces by -0.2 (the OI model suggests the extreme may not persist).
- **Idiosyncratic check**: Other assets in the same sector showing normal funding adds +0.2 (idiosyncratic dislocation is more likely to revert quickly). Sector-wide funding dislocation reduces by -0.2 (systematic positioning, slower to revert).
- **Event calendar check**: No major scheduled event for the asset in the next 4 hours adds +0.1. A known event within 4 hours reduces by -0.4 (events override funding dynamics).

### When Data Is Missing

If funding rate data is unavailable for the current period, do not enter — funding is the sole signal for this thesis. If fewer than 14 days of funding history are available, use whatever history exists with a -0.1 uncertainty penalty per missing day below 14 (maximum -0.5). If OI data is unavailable, skip the OI-funding alignment check entirely (treat as 0.0) and apply the uncertainty penalty noted above. Predicted funding is a derived signal: if it cannot be computed, skip it with no penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

## Position Parameters

- Direction: For positive extreme: Short (fade the long premium).
  For negative extreme: Long (fade the short premium)
- Leverage: 4x (higher leverage because mean reversion is time-bound
  by funding settlement)
- Position size: 10% of account per trade
- Stop loss: 2.0% from entry (tight -- if funding doesn't revert soon,
  the thesis is wrong)
- Take profit: Exit when funding z-score returns to within ±1.0
  (the signal resolves, not a price target)
- Max hold time: Until next funding settlement (max 8 hours)

## Known Weaknesses

- In persistent trends, funding can stay extreme for days -- this
  agent bleeds to the trend
- Most correlated to `jade_hawk` (VWAP mean reversion) -- both fade
  extremes, jade_hawk from price and this agent from funding
- Low volatility regimes with stable funding produce no signals
- Gap risk: funding normalises via a sharp move that hits the SL
  first, then continues in the thesis direction

## Assets in Focus

Primary: SOL, ETH, ARB, OP, SUI (high funding variance)
Secondary: BTC (lower funding variance but deeper liquidity)
Avoid: PEPE, DOGE, WIF (funding too noisy, low predictive value)
