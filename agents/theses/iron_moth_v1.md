# iron_moth -- Thesis v1: Cross-sectional Momentum

## Edge Hypothesis

Cross-sectional momentum across the perpetuals universe produces persistent, uncorrelated alpha that is independent of absolute market direction. By ranking all assets in the universe on multiple return horizons (30m, 2h, 12h, 24h) and entering the strongest relative performers, the agent captures the tendency of winning assets to continue outperforming losers within the same asset class -- the textbook momentum premium adapted for crypto perps.

Momentum alone is noisy. The edge is sharpened by requiring momentum acceleration (the most recent horizon > longer horizons) and volatility-adjusted returns (Sharpe ratio of the momentum signal over the lookback). Sector-relative momentum excludes trades that are just beta -- if all assets are rising, the agent prioritises assets rising the most within their sector peer group. Funding rate and order book data are explicitly ignored to keep the signal pure.

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Composite momentum rank**: Asset rank in the universe by weighted momentum score. Top 3 = strong +0.7, rank 4-5 = moderate +0.4, rank 6-10 = weak +0.1. Outside the top 10, the signal is too diluted to contribute meaningful conviction (+0.0).
- **Momentum acceleration**: Most recent horizon return exceeding the average of all longer horizons. Clear acceleration adds +0.5. Flat acceleration (recent horizon roughly equal to longer horizons) adds +0.2. Deceleration (recent horizon below longer averages) reduces confidence by -0.4 — this is a warning the momentum is fading.
- **Volatility-adjusted return**: Return divided by ATR over the lookback. Ratio > 0.5 = strong +0.6, 0.3-0.5 = moderate +0.3, 0.15-0.3 = weak +0.1. Below 0.15 the signal is noise-dominated and contributes nothing.
- **Sector-relative rank**: Top 2 within the asset's sector peer group adds +0.4. Rank 3-4 within sector adds +0.2. Bottom of the sector (despite high absolute rank) reduces by -0.3 — the asset is riding beta, not alpha.

### Secondary Evidence (moderate weight)

- **BTC correlation discount**: If the asset is not the highest-correlation member of its sector to BTC, add +0.2 (idiosyncratic momentum, not beta). If it is the highest-correlation member, reduce by -0.2.
- **Volume confirmation**: Volume above the 14-day median adds +0.2. Below the median reduces by -0.2 (momentum without participation is suspect).
- **Regime compatibility**: No conflicting regime tag (e.g. avoid fresh crisis regime) adds +0.1. Crisis or range_high_vol regimes reduce momentum reliability by -0.3.

### When Data Is Missing

If return data for one or more horizons is unavailable for a given asset, exclude that horizon from the composite score and note the reduction. If two or more horizons are missing, the asset cannot be reliably ranked — remove it from the universe for that cycle. Volume data missing defaults to neutral (0.0 instead of the secondary contribution). If sector classification is unavailable, skip the sector-relative rank check entirely and apply a -0.1 uncertainty penalty.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait

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
