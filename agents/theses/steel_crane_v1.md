# steel_crane -- Thesis v1: Liquidation Hunter

## Edge Hypothesis

Crypto perpetual markets generate a unique and exploitable signal: forced liquidations. When leveraged positions are forcibly closed, the mechanical cascade creates temporary price dislocations that are both larger and more predictable than the efficient market hypothesis would predict. Liquidations create the opposite signal: long liquidations create downside pressure and then a vacuum that pulls price back; short liquidations create upside fuel.

This agent monitors liquidation clusters (concentrated volumes at specific price levels), cascade history (chain liquidations that snowball as each liquidation pushes price further), leverage estimates from OI/price ratios (when estimated leverage is extreme, liquidations become more likely), funding rates (trapped longs paying high funding while underwater are prime cascade targets), and OI changes in the wake of liquidation events. The core question: is a squeeze likely? Has one already happened? If the cascade is in progress, the agent fades it -- enters against the cascade direction expecting reversion as the book rebalances.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Liquidation volume magnitude**: Single-asset liquidation volume in the last 15 minutes. > $10M = strong +0.8, $5-10M = moderate +0.6, $2-5M = weak +0.3. Below $2M the cascade signal is too small to reliably fade — contribute 0.0. Volume direction determines sign: long liquidations = short-term downside, then fade long.
- **Price move magnitude**: Price movement in the direction of the dominant liquidation type. > 3% = strong +0.6, 2-3% = moderate +0.4, 1-2% = weak +0.2. Below 1% the price hasn't reacted to the liquidations — contributes 0.0.
- **Estimated leverage**: Current estimated leverage (OI / notional value) for the asset. > 20x = strong +0.5, 15-20x = moderate +0.3, 10-15x = weak +0.1. Below 10x contributes nothing. Higher leverage means more fuel remains for the cascade.
- **OI drawdown during cascade**: OI drop during the cascade event. > 5% drop = strong +0.6 (significant positions removed), 3-5% drop = moderate +0.4, 1-3% drop = weak +0.1. Below 1% OI change suggests the cascade is not materially reducing open positions — reduce by -0.2.

### Secondary Evidence (moderate weight)

- **Pre-cascade funding extremity**: Funding rate was extreme (z-score > 1.5 or < -1.5) before the cascade adds +0.3. Trapped positions paying to stay in are prime cascade fuel. Neutral pre-cascade funding contributes nothing.
- **Liquidation cluster concentration**: Multiple liquidation clusters visible on the book adds +0.3 (cascades propagate through clustered levels). A single liquidation level adds +0.1. No visible clusters reduce by -0.2.
- **Idiosyncratic check**: Other assets in the same sector are not experiencing cascades adds +0.2 (idiosyncratic event, safer to fade). Sector-wide cascades reduce by -0.3 (systemic risk overwhelms the fade thesis).
- **Regime compatibility**: Regime tag is not crisis adds +0.2. Crisis regime reduces by -0.5 — systemic cascades behave differently and the fade is much riskier.

### When Data Is Missing

If liquidation volume data is unavailable, this thesis has no edge — do not enter. If OI data is unavailable, the OI drawdown and estimated leverage pillars are removed: maximum achievable confidence drops to ~50%. If funding rate data is unavailable, skip the pre-cascade funding check with no penalty. If liquidation cluster data is unavailable (book data missing), skip with no penalty. If regime tag is unavailable, assume non-crisis (most conservative assumption) with no penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

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
