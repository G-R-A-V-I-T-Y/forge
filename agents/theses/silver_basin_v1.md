# silver_basin -- Thesis v1: Funding Dislocation

## Edge Hypothesis

Funding rates are the purest expression of leverage demand in the crypto perpetuals market. When traders are willing to pay extreme premiums (positive funding) or receive extreme discounts (negative funding), they are expressing a conviction that is statistically likely to be wrong at the extreme. Funding dislocations are self-correcting: the cost of holding a trade mechanically reduces the edge of the crowded side until it unwinds.

This agent studies nothing but funding. It computes the current funding rate z-score vs a rolling 14-day history, the funding trend (direction of the last 3 periods), predicted funding from OI changes (when OI surges, funding tends to follow), and funding acceleration (the rate of change of funding itself). Entry is triggered when funding is statistically irrational -- extreme z-scores combined with accelerating trend -- and held until funding normalises. Price action is deliberately excluded to avoid confirmation bias.

## Entry Conditions

**Required (all must be met):
1. Funding rate z-score > 2.0 (positive extreme) or < -2.0 (negative
   extreme) against 14-day rolling window
2. Funding acceleration confirms the extreme -- the last period's
   change is in the same direction as the dislocation
3. OI change supports the thesis: for positive funding extreme,
   OI should be flat or falling (late-stage crowding); for negative
   funding extreme, OI should be stable or rising (building shorts)
4. No major scheduled event for the asset in the next 4 hours
   (earnings, major unlocks, regulatory decisions)

**Supporting (raise confidence, not required):**
- Funding has been extreme for 2+ consecutive periods (persistence)
- Predicted funding from OI model agrees with current extreme
- Other assets in same sector show normal funding (idiosyncratic)

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
- Most correlated to `jade_hawk` (regime detector) -- extreme funding
  is often a regime signal itself
- Low volatility regimes with stable funding produce no signals
- Gap risk: funding normalises via a sharp move that hits the SL
  first, then continues in the thesis direction

## Assets in Focus

Primary: SOL, ETH, ARB, OP, SUI (high funding variance)
Secondary: BTC (lower funding variance but deeper liquidity)
Avoid: PEPE, DOGE, WIF (funding too noisy, low predictive value)
