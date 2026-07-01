# crimson_fox -- Thesis v1: Meta Agent

## Edge Hypothesis

The desk's collective historical performance contains latent patterns that no single strategy can see. Which agent performs best in which market regime? Which conditions produce the highest Sharpe for each strategy? How should confidence be allocated across the desk based on current market conditions? These questions define the meta-agent's domain -- it does not study markets, it studies the other nine agents.

crimson_fox analyses the full historical trade bank to learn which strategies produce edge in which regimes. It builds and maintains a performance matrix: for each known regime tag (from jade_hawk), it computes the win rate, Sharpe ratio, profit factor, and average return of every other agent. It then identifies which strategies are currently working, which are not, and which conditions signal a likely regime transition. Its output is a set of confidence multipliers for each other agent -- a recommendation of how much to trust each strategy given the current market state.

Over time, crimson_fox develops a meta-model: "In trending_bull regimes, iron_moth has a 68% win rate with 1.9 Sharpe. When funding is extreme, silver_basin's mean-reversion produces 72% win rates but only in the first 4 hours. In low_vol regimes, gray_finch and
amber_wolf both underperform -- reduce allocation to 0.5x." This
meta-knowledge may become the highest-Sharpe signal on the desk,
as it compounds the edge of every other strategy through dynamic
allocation.

## Entry Conditions

**crimson_fox does not enter trades directly.** It enters an analysis cycle:
1. On every desk evaluation cycle (every 6h) or on demand via
   meta-controller trigger
2. When the trade bank has accumulated 10+ new closed trades since
   the last analysis
3. When the regime tag changes (jade_hawk posts a new classification)

**Required data for each analysis cycle:**
- All closed trades across all agents (last 7 days window minimum,
  expanding to full history when meaningful)
- Current regime tag + confidence from jade_hawk
- Per-agent performance slices by regime (historical)
- Per-agent performance slices by funding rate environment
- Per-agent performance slices by volatility regime (from violet_lion)

## Output

A structured confidence report written to the database and broadcast
to all agents at decision time:

```json
{
  "as_of": "2026-06-30T12:00:00Z",
  "regime": "trending_bull",
  "regime_confidence": 0.82,
  "multipliers": {
    "iron_moth": 1.2,
    "silver_basin": 0.6,
    "copper_vane": 1.0,
    "gray_finch": 1.1,
    "amber_wolf": 1.0,
    "steel_crane": 0.8,
    "onyx_heron": 0.5,
    "jade_hawk": 1.0,
    "violet_lion": 1.0
  },
  "reasoning": "In trending_bull regimes (N=85 trades over 14
    days), iron_moth shows 1.9 Sharpe and 68% WR -- momentum over-
    performs. silver_basin has 0.8 Sharpe -- funding extremes are
    less reliable in strong trends. onyx_heron is at 0.5x as mean-
    reversion pairs underperform in directional markets.",
  "analysis_version": 12
}
```

## Output Integration

- Every other agent receives its multiplier in the decision prompt:
  "crimson_fox confidence multiplier for your strategy: 1.2x"
- Agents are encouraged (not required) to scale their conviction by
  this multiplier when sizing
- The risk gate does not enforce multipliers -- they are advisory
- The meta-controller can use the confidence report to prioritise
  which agents to evaluate first or allocate more compute to

## Known Weaknesses

- Needs a critical mass of trades before its outputs are meaningful
  (100+ across the desk minimum)
- In a regime shift, historical performance is a liability -- the
  matrix takes time to re-calibrate
- Over-optimisation risk: if the trade bank is small, multipliers
  can overfit to noise
- Most exposed to shared failure modes: if all agents are wrong
  in the same way, the meta-agent compounds the error
- The meta-agent is the most complex agent and the hardest to
  validate -- wrong multipliers can damage the whole desk
