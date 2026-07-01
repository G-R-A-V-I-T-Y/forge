# crimson_fox -- Thesis v1: Session Pattern Arbitrage

## Edge Hypothesis

Crypto perpetuals trade 24/7 across global sessions, but participation, order flow, and directional bias follow predictable daily and weekly patterns. These time-based patterns exist because different sets of participants dominate at different hours: APAC retail and systematic flows in the Asian session, European institutions during London hours, US leveraged players at the NY open. Each transition between these participant cohorts creates exploitable directional drift.

The edge is small per trade but highly reliable and available every single day. This is a compounding strategy, not a home-run strategy. The agent does not predict market direction from price action or volume -- it knows what time it is and what statistically tends to happen at that time.

Trading is restricted to specific session windows where historical edge has been established. Outside these windows the agent waits.

## Session Definitions (all times UTC)

### US Open (12:00--13:30 UTC, Mon--Fri)
The highest-volume 90 minutes of the day. BTC and alts frequently gap or break in the first 30 minutes of US cash equities opening. The edge: trade the direction of the first 15-minute candle with a tight stop. If the first 15m candle is green, go long; if red, go short. The US open directional bias has persistence for 60--90 minutes on ~60% of trading days.

### US Afternoon Reversal (16:00--18:00 UTC, Mon--Fri)
The 'puppet show' period where algo desks and institutional flow push into the close. Statistically significant reversal from the US morning trend. If price moved up during the US session (12:00--16:00), expect a mean-reverting pullback. If price moved down, expect a bounce. This is the highest-Sharpe session window.

### Asian Session Drift (00:00--02:00 UTC, Mon--Fri)
Lower liquidity, wider spreads, but consistent directional drift from systematic APAC flows. The edge: trade the direction of the first 30-minute candle of the Asian session. The drift tends to persist for 1--3 hours and is most reliable when it follows a quiet US session (low volatility carries through).

### Weekly Patterns
- **Monday open (00:00 UTC)**: Weekend gap fills are common in the first 4 hours. If BTC gapped up over the weekend, expect a fill-down; if gapped down, expect a fill-up.
- **Friday afternoon (14:00--18:00 UTC)**: Position squaring before the weekend. Longs are closed, shorts are covered. Creates mean-reverting moves.
- **Funding settlement windows (00:00, 08:00, 16:00 UTC)**: Positioning changes 30--60 minutes before settlement as traders adjust leveraged positions. Mild directional bias in the hour leading into settlement.

## Entry Conditions

**Required (all must be met):**
1. Current time falls within a defined session window (US Open, US Reversal, Asian Drift, or weekly pattern)
2. The session has a direction signal based on its rule (first candle direction, session trend reversal, gap fill)
3. Volume confirms participation is at expected session levels (not a holiday/thin session)
4. No major scheduled event for BTC or any target asset in the next 2 hours (macro news, CPI, FOMC, major unlocks)

**Supporting (raise confidence, not required):**
- The session signal aligns with the market regime (e.g., trend regime + US Open direction = strong confluence)
- The prior session showed low volatility (quiet carry-through increases pattern reliability)
- Multiple session patterns align simultaneously (e.g., it's Monday US Open AND a weekend gap exists)
- Funding is neutral or supports the session direction

## Position Parameters

- Direction: Per session rule. US Open: direction of first 15m candle. US Reversal: counter to session trend. Asian Drift: direction of first 30m candle. Weekly: gap fill direction.
- Leverage: 2x (lower -- time-based edges are small but consistent)
- Position size: 8% of account per trade
- Stop loss: 1.0% from entry (tight -- time-based edge is invalidated quickly if wrong)
- Take profit: Session exit condition (TP at 1.5% or session end, whichever comes first)
- Max hold time: Until the session window closes (maximum 4 hours for Asian drift, 90 minutes for US Open)

## Entry Rules Summary

| Session | When (UTC) | Entry Rule | Max Hold |
|---------|-----------|------------|----------|
| US Open | Mon--Fri 12:00 | Direction of first 15m candle | 90 min |
| US Reversal | Mon--Fri 16:00 | Counter to US session trend (12:00--16:00) | 2h |
| Asian Drift | Mon--Fri 00:00 | Direction of first 30m candle | 3h |
| Monday Gap | Mon 00:00--04:00 | Counter to weekend gap direction | 4h |
| Friday Squaring | Fri 14:00--18:00 | Counter to week's last 4h direction | 4h |
| Pre-settlement | 07:00--08:00, 15:00--16:00, 23:00--00:00 | Direction of improving funding rate | 1h |

## Known Weaknesses

- Small edge per trade requires many repetitions -- statistical significance takes weeks of trading
- Major macro news (FOMC, CPI, NFP) completely overrides session patterns -- must flat through known events
- DST transitions and holiday weeks shift session boundaries -- reduced reliability during transitions
- Weekend sessions (Saturday--Sunday) do not have reliable patterns -- agent skips them entirely
- Most vulnerable to structural market changes that break historical patterns (e.g., ETF approval changing US open behaviour permanently)
- Lowest Sharpe agent on the desk individually -- value is as a uncorrelated compounding machine alongside the other strategies
- Session boundaries are approximate; exact timing shifts with market structure -- requires periodic re-calibration

## Assets in Focus

Primary: BTC, ETH (most consistent session behaviour across all time windows)
Secondary: SOL (growing session consistency, particularly in US hours)
Avoid: Small-cap perps -- session patterns are unreliable when the asset itself drives the flow rather than macro session dynamics
