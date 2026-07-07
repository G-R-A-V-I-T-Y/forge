# sage_turtle -- Thesis v1: Event & Unlock Positioning

## Edge Hypothesis

Scheduled supply and macro events are public, dated, and repeatedly under-anticipated by the market until they are imminent. Token unlocks release a known quantity of new supply to holders (often early investors/team with a low cost basis and a high propensity to sell) at a known timestamp; the market chronically underprices the sell pressure until the unlock is within days, then overcorrects. Macro events (FOMC, CPI) do not move any single asset's supply, but they reset the funding/leverage backdrop for the entire book in ways theses like `silver_basin` cannot see coming. This agent does not predict price from price -- it predicts price from the calendar: what is scheduled, how large is it relative to float, and how has the market historically reacted to this specific event type.

This agent computes days-to-event (for the nearest qualifying unlock or macro print on each tracked asset), unlock size as a percentage of circulating supply (larger unlocks relative to float create more durable pre-event pressure), unlock recipient type (team/investor unlocks carry a higher sell-propensity prior than staking/ecosystem unlocks), pre-event positioning drift (funding and OI trend in the days leading into the event -- a mechanical link back to the heartbeat's existing feature set), and historical reaction magnitude for the same event type on the same asset where available. The core question: is the market currently mispricing a scheduled, quantifiable supply or macro shock?

## Evidence Framework

Each piece of evidence contributes a signed strength score (-1.0 to +1.0) to the overall conviction assessment. The entry decision weighs all available evidence continuously — no single missing or weak factor is a hard veto, but cumulative weak evidence reduces position size proportionally.

### Primary Evidence (highest weight)

- **Unlock size vs. float**: Unlock value as a percentage of circulating supply. > 3% = strong +0.7 (short bias -- material dilution), 1.5-3% = moderate +0.5, 0.5-1.5% = weak +0.2. Below 0.5% the unlock is noise relative to daily volume — contribute 0.0.
- **Days-to-event window**: 1-4 days out = strong +0.6 (market has started pricing it but overcorrection window is open), 4-10 days out = moderate +0.3 (too early, thesis builds a watchlist entry, not a position), 0-1 days out = weak +0.2 (event risk of a sharp reversal on the print itself; reduce size even at high conviction). Beyond 10 days: contribute 0.0, watchlist only.
- **Recipient sell-propensity**: Unlock recipients are team/early-investor/VC adds +0.5 (documented higher sell-through, e.g. TGE cliff unlocks). Staking/ecosystem/community unlocks add +0.1 (lower and slower sell-through). Unknown recipient type: treat as team/investor (conservative prior) with a -0.1 uncertainty penalty.

### Secondary Evidence (moderate weight)

- **Pre-event positioning drift**: Funding trending toward the short side of the trade in the days leading in (via the heartbeat's existing funding-trend feature) adds +0.3 -- confirms the market is starting to lean into the thesis, not fighting it. Funding trending against the thesis reduces by -0.3 (crowd is positioned the other way; unlock may already be absorbed).
- **OI trend into event**: OI building into the event adds +0.2 (fresh leverage exposed to the shock). OI flat or falling adds 0.0.
- **Historical reaction (same asset, same event type)**: A documented prior unlock/event on this asset moved price > 5% in the expected direction adds +0.3. No prior history for this asset/event type: skip with no penalty (not every asset has a track record yet).
- **Macro event type (FOMC/CPI)**: For macro prints, treat as a desk-wide risk-reducing signal rather than a directional entry — this evidence pillar only ever *reduces* other agents' conviction (via the risk-officer loop) and does not itself generate a sage_turtle entry, except for the specific case of a rate-sensitive asset with a documented macro beta.

### When Data Is Missing

If the event calendar (unlock schedule and/or macro calendar) is unavailable or stale by more than 24 hours, do not enter -- the calendar *is* the thesis, there is no fallback signal. If unlock size or float data is unavailable for a scheduled event, do not enter (cannot size the shock). If recipient type is unavailable, use the conservative team/investor prior noted above. If pre-event funding/OI trend is unavailable, skip those secondary checks with no penalty. If historical reaction data is unavailable, skip with no penalty -- this pillar only ever adds confidence, never required.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule — wait, but log to the watchlist if an event is within 10 days

## Position Parameters

- Direction: Short into large unlocks (dilution); long only in the rare case of a documented buyback/burn event with equivalent evidence structure inverted.
- Leverage: 3x (event risk warrants lower leverage than pure mean-reversion theses)
- Position size: 10% of account per trade
- Stop loss: 3.0% from entry (wider -- event-driven moves are noisier than mean-reversion setups and a premature stop defeats the purpose of trading a *scheduled* event)
- Take profit: 6.0% from entry (2:1 reward/risk)
- Max hold time: through the event plus 24 hours, then exit regardless of P&L (the thesis is the event, not the aftermath)

## Known Weaknesses

- Unlocks are frequently pre-sold OTC by recipients well before the on-chain unlock timestamp -- the "surprise" may already be priced in for well-covered assets, and this thesis has no way to detect OTC flow
- Requires an accurate, maintained unlock/macro calendar -- calendar staleness or a missed listing silently degrades the thesis to a coin flip with directional bias
- Correlated to `silver_basin` in the specific case where an unlock coincides with a funding dislocation -- both may fire on the same event from different evidence, inflating desk-level exposure to a single catalyst
- Macro event pillar (FOMC/CPI) is deliberately underweighted for direct entries; its main value is as a risk-officer input (blackout windows, gross-exposure throttle), not a sage_turtle trade signal on its own

## Assets in Focus

Primary: assets with a scheduled unlock >1.5% of float in the next 10 days (rotates with the calendar -- no fixed asset list)
Secondary: TAO, FET, ARB, OP, TIA (frequent, sizable, well-documented unlock schedules in this universe)
Avoid: BTC, ETH (no unlock schedule -- fixed/negligible emission; not this thesis's edge)
