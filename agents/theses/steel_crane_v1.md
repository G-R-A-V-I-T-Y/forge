# steel_crane -- Thesis v1: Liquidation Hunter

## Edge Hypothesis

Crypto perpetual markets generate a unique and exploitable signal: forced liquidations. When leveraged positions are forcibly closed, the mechanical cascade creates temporary price dislocations that are both larger and more predictable than the efficient market hypothesis would predict. Liquidations create the opposite signal: long liquidations create downside pressure and then a vacuum that pulls price back; short liquidations create upside fuel.

This agent monitors liquidation clusters (concentrated volumes at specific price levels), cascade history (chain liquidations that snowball as each liquidation pushes price further), leverage estimates from OI/price ratios (when estimated leverage is extreme, liquidations become more likely), funding rates (trapped longs paying high funding while underwater are prime cascade targets), and OI changes in the wake of liquidation events. The core question: is a squeeze likely? Has one already happened? If the cascade is in progress, the agent fades it -- enters against the cascade direction expecting reversion as the book rebalances.

## Entry Conditions

**Required (all must be met):**
1. Single-asset liquidation volume > $5M USD in the last 15 minutes
   (meaningful cascade, not routine liquidations)
2. Price has moved > 2% in the direction of the dominant liquidation
   type (longs getting liquidated → price down; shorts getting
   liquidated → price up)
3. Estimated leverage (OI / notional value) > 15x for the asset
   (extreme leverage = more fuel for the cascade)
4. OI has dropped > 3% during the cascade (positions are being
   removed from the book, reducing selling/buying pressure)

**Supporting (raise confidence, not required):**
- Funding rate was extreme before the cascade (funding + cascade
  confluences -- trapped positions are paying to stay in)
- Multiple liquidation clusters visible on the book (cascades tend
  to propagate through clustered liquidity)
- Other assets in the same sector are not experiencing cascades
  (idiosyncratic event, not systemic)
- Regime tag is not crisis (systemic cascades are different)

## Position Parameters

- Direction: Counter-cascade. Long liquidations → Long.
  Short liquidations → Short.
- Leverage: 4x (higher leverage to compensate for rare-event
  frequency -- cascades happen infrequently but have high edge)
- Position size: 8% per trade (conservative -- cascade timing is
  notoriously difficult)
- Stop loss: 2.0% from entry
- Take profit: 4.0% from entry (2:1 reward/risk)
- Max hold time: 4 hours (cascades resolve quickly; if the
  reversion hasn't happened in 4h, the thesis was wrong)

## Known Weaknesses

- Cascades can compound (cascading liquidations that trigger more
  liquidations) -- fading early gets run over
- False cascades: a large single liquidation can look like a
  cascade; need cluster detection to distinguish
- Most effective on mid-cap perps (SOL, SUI, ARB) where liquidation
  impact on price is highest
- BTC cascades are deeper but fade more quickly -- tighter edge

## Assets in Focus

Primary: SOL, SUI, ARB, OP, ETH (active liquidation markets)
Secondary: BTC (deep but spreads edge thin)
Avoid: Low-OI assets with <$10M daily liquidation volume
