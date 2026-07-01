# iron_moth -- Thesis v1: Cross-sectional Momentum

## Edge Hypothesis

Cross-sectional momentum across the perpetuals universe produces persistent, uncorrelated alpha that is independent of absolute market direction. By ranking all assets in the universe on multiple return horizons (30m, 2h, 12h, 24h) and entering the strongest relative performers, the agent captures the tendency of winning assets to continue outperforming losers within the same asset class -- the textbook momentum premium adapted for crypto perps.

Momentum alone is noisy. The edge is sharpened by requiring momentum acceleration (the most recent horizon > longer horizons) and volatility-adjusted returns (Sharpe ratio of the momentum signal over the lookback). Sector-relative momentum excludes trades that are just beta -- if all assets are rising, the agent prioritises assets rising the most within their sector peer group. Funding rate and order book data are explicitly ignored to keep the signal pure.

## Entry Conditions

**Required (all must be met):**
1. Asset ranks in top 3 of universe by composite momentum score
   (weighted average of normalized 30m, 2h, 12h, 24h returns)
2. Momentum acceleration confirmed: most recent horizon return
   exceeds the average of all longer horizons
3. Volatility-adjusted return > 0.5 (return / ATR over lookback)
4. Sector-relative rank is top 2 within the asset's sector peer group

**Supporting (raise confidence, not required):**
- Asset is not the highest-correlation member of its sector to BTC
- Volume is above the 14-day median (confirms participation)
- No conflicting regime tag (e.g. avoid fresh crisis regime)

## Position Parameters

- Direction: Long only (cross-sectional momentum on perps favors
  the long side due to funding cost on shorts)
- Leverage: 3x
- Position size: 12% of account per trade
- Stop loss: 2.5% below entry price
- Take profit: 5.0% above entry price (2:1 reward/risk)
- Max hold time: 12 hours; if TP not hit, re-evaluate at next wake

## Known Weaknesses

- Underperforms during sharp trend reversals when momentum flips
- High correlation to other long-biased strategies on the desk
- Mean-reversion regimes (range_high_vol) generate whipsaws
- In low-vol regimes the ranking becomes noise-dominated

## Assets in Focus

Primary: SOL, ETH, SUI, AVAX, LINK (higher momentum dispersion)
Secondary: BTC (lower momentum variance but anchor position)
Avoid: Stablecoin pairs and assets with <$50M daily volume
