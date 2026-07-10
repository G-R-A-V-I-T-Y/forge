# Forge — Strategic Assessment & Development Path

**Date:** 2026-07-04
**Scope:** Full review of the proposal (`docs/FORGE_PROPOSAL.md`), all code (M1–M5 complete), the live database (65 trades, 2026-07-02 → 2026-07-04), theses, and the in-flight live-trading work.
**Question answered:** What should Forge become to have the highest likelihood of making money daily, autonomously, and durably — and what is the concrete path there?

---

## 1. Executive Summary

Forge's core wager — an evolutionary ecosystem of AI traders competing under prop-shop discipline, where the *system* learns even when individual strategies decay — is the right wager. Ecosystems outlive strategies. The institutional-memory design (fingerprints, graveyard, cross-agent queries) is genuinely differentiated and worth protecting.

But three findings from this review change what should be built next:

1. **The current results are not real.** The leaderboard's top agent (silver_basin, +$2,000) earned most of its PnL from a risk-gate hole: the gate never checks that a stop-loss is on the *losing* side of entry, so wrong-side stops book instant profits at prices that never traded (one trade: +25.5% in 3 minutes at a fill 6.4% away from the market). Six of 45 closed trades are corrupted. Until measurement integrity is fixed, every downstream mechanism — evolution, culling, promotion — would be selecting on bugs, not edge.

2. **Attribution is destroyed by the model fallback chain.** Decisions were made by whichever free model happened to answer: 31 trades by "North Mini Code Free," 24 by local Qwen, 9 by MiMo, 1 by Nemotron. An agent's track record is currently `f(thesis × random model × market)`. Evolution cannot work when the genome changes randomly per decision.

3. **The LLM is deployed at the wrong timescale.** Having a 35B model re-read 10,800 tokens of numbers every 5 minutes to recompute what a z-score means is the weakest possible use of LLM intelligence — slow, expensive, noisy, and *impossible to backtest*. The strongest use is the one the proposal already gestures at but hasn't built: the reflection loop. LLMs should do the *slow thinking* (hypothesis generation, strategy synthesis, postmortem reasoning, regime interpretation) and emit *fast artifacts* (executable, backtestable strategy specs) that trade mechanically.

**The verdict on the core premise** (§3): handing numerical state to a frontier-class reasoner and expecting strategic judgment is *not* false — but it is only true at the timescales where reasoning beats arithmetic. The Slay the Spire result is real, and it transfers to markets in exactly one place: slow, structural, mechanism-driven decisions (funding regimes, liquidation reflexivity, event positioning, strategy design). It does not transfer to 5-minute signal evaluation, where the LLM is a slow, lossy calculator adding noise to features the heartbeat already computed. Forge should keep LLM agency — but move it up one level of abstraction, and let the arena itself settle the question empirically by running both paradigms head-to-head.

The recommended path: **fix truth first (M6), build memory and a backtest engine (M7), then build the evolution loop as the product (M8–M9), then go live small (M10–M11).** Detailed milestones with tasks in §8.

---

## 2. Where the System Actually Stands

### What's built and working

- **Architecture skeleton is sound.** Heartbeat-as-shared-snapshot (one API sweep per 5 min, atomic file, staleness checks) is the right pattern and is well-engineered. Agents-as-subprocesses gives real parallelism. The bridge abstraction (paper/live behind one interface) is correctly load-bearing for the whole promotion story.
- **Feature computation is substantial.** ~40 per-asset fields (returns, funding z-score, OI z-score, book depth, aggressor ratio, liquidation-cascade proxy, resampled candles), cross-asset (correlation matrix, PCA, sector strength, breadth), regime tagging. This is more market context than most retail systems ever assemble.
- **Fingerprint store + query layer** (M4) exists and is wired into prompts (own history under similar conditions + cross-agent pattern reference). This is the seed of the institutional-memory moat.
- **Theses are high quality.** The probabilistic evidence framework (signed evidence strengths, confidence-scaled sizing, explicit missing-data handling, known weaknesses) is a genuinely good prompt-engineering pattern.
- **Live-trading plumbing is started** (untracked: `execution/live_bridge.py`, per-agent keystores, `scripts/onboard_trader.py`) — EIP-712 signing, IOC orders, circuit breaker.

### What the 65 trades actually say

| Agent | Closed | Wins | Net PnL | Note |
|---|---|---|---|---|
| silver_basin | 11 | 6 | +$2,000 | **~$1,400+ of this is phantom** (wrong-side SL/TP fills; see §4.1) |
| iron_moth | 2 | 2 | +$1,188 | 2 trades — pure luck territory |
| gray_finch | 12 | 6 | +$411 | microstructure thesis on 5-min-stale data (§5) |
| jade_hawk | 7 | 1 | −$972 | |
| violet_lion | 7 | 2 | −$139 | |
| copper_vane / onyx_heron / crimson_fox | 6 | 0 | −$665 | |
| amber_wolf / steel_crane | 0 | — | — | never closed a trade |

Aggregate: roughly flat (+$1.8k on $500k paper, ≈+0.36%), over 2.5 days, in one regime (`range_high_vol`), with corrupted PnL in the winner. **There is no signal here yet — positive or negative.** That's expected at this stage; the problem is only that the system currently has no way to *know* that. Reflections: 0. Evaluations: 0. "Wait" decisions: not persisted at all (selection bias baked into the record — see §4.4).

Also observed in the data: stop-losses fire at 2.4× the average magnitude that take-profits win (−2.36% avg vs +5.75% avg is fine, but 27 SL vs 18 TP with the SL exits clustering in fast reversals suggests stops are being set inside 5-minute noise — a cadence problem, not a thesis problem).

---

## 3. The Central Question: Is "Numbers → LLM → Trades" a False Premise?

This deserves a direct answer, because the whole roadmap depends on it.

### Where the premise is weak

- **At the 5-minute decision cadence, the LLM adds noise, not intelligence.** Every input it sees (funding z-score, RSI, depth imbalance) was computed mechanically by the heartbeat. The LLM's marginal contribution is to *weigh* them — a task where a logistic regression fit on 500 fingerprints will beat a 35B model reading a text table, at 1/10,000th the cost. LLMs are demonstrably poor at consistent numerical weighing: the same prompt yields different trades at temperature > 0, and different *models* (see the fallback chain) yield wildly different trades. What we saw in the DB confirms it: gray_finch entered on a "36:1 bid/ask depth ratio" — a top-5-levels artifact that was stale within seconds — because the number was big, with no ability to know the number was garbage.
- **LLM decisions cannot be backtested.** This is the single most expensive property of the current design. A mechanical strategy can be validated against two years of candles in seconds; an LLM-in-the-loop strategy can only be validated by *living through* market time, at ~10 trades/agent/week. At that sample rate, distinguishing a 55%-win-rate agent from a coin flip takes months per thesis version. Evolution at that generation time is glacial.
- **The Slay the Spire analogy breaks in three places.** StS is stationary (rules never change), single-player (the environment doesn't adapt to exploit you), and fully observable. Markets are none of these. The information in a screenshot of a deck is *not priced in*; the information in a funding z-score is seen simultaneously by every quant desk on earth. Public numbers handed to a reasoner are mostly already in the price.

### Where the premise is strong — and it is genuinely strong

- **Perpetuals are a rule-driven machine, and LLMs reason well about machines.** Funding mechanics, liquidation ladders, forced-flow reflexivity ("shorts paying 4σ funding must either close or bleed; OI falling means they're closing; that flow is buy-side") — this is *causal, mechanistic* reasoning about known system dynamics, exactly what the proposal's jade_hawk example celebrates and exactly what LLMs do better than any regression. The best hypotheses in the trade bank (silver_basin's funding-dislocation reasoning) read like a competent trader because *this kind* of thinking is language-shaped.
- **Strategy design is a language task.** Generating a novel thesis, criticizing it adversarially, noticing that wins cluster in one regime, proposing a parameter change, checking it against the graveyard — the entire evolutionary layer of Forge is LLM-shaped. This is where the "identify non-obvious synergies" magic actually lives.
- **Regime synthesis is a language task.** "ETF outflows three days running, funding flat, breadth collapsing, an FOMC meeting in 30 hours" → "cut gross, disable breakout agents" is judgment over heterogeneous evidence — the thing markets pay discretionary PMs for, and the thing pure mechanical systems handle worst.

### Verdict and the architectural consequence

**The premise is half-right, and the half matters.** LLM-as-tick-trader is a weak paradigm. LLM-as-strategist/evolver/risk-officer over a mechanical execution layer is a strong one — arguably the strongest new paradigm available to a solo operator, because it automates the one thing quant shops can't scale: hypothesis generation and honest postmortem reasoning.

The consequence: **split every agent into a slow mind and a fast body.**

- The *mind* (LLM, runs at reflection cadence — hours/days): owns the thesis, reads the trade bank and backtests, does research, writes and revises a **strategy spec** — a constrained, executable artifact (signals, thresholds, entry/exit/sizing rules, regime filters).
- The *body* (deterministic Python, runs at heartbeat cadence): executes the spec mechanically. Fast, free, consistent, and — critically — **backtestable**, so every proposed thesis revision is validated against history *before* it risks even paper capital.

This is not abandoning the vision; it is the prop-shop model taken seriously. Real traders don't recompute their indicators by hand every 5 minutes — they design a playbook and execute it with discipline, revising it in the evening. The persona framing gets *more* honest, not less.

And because the question deserves an empirical answer rather than an architectural decree: **keep 2–3 pure LLM-decides agents in the arena** (with pinned models, temperature 0, and full decision logging) as a control arm. If the reasoning agents beat the compiled agents on risk-adjusted net PnL over 500+ trades, the ecosystem will say so. Forge's whole point is that the market decides.

---

## 4. Integrity Defects — Fix Before Anything Else Matters

These are ordered by severity. Every one of them corrupts the signal the evolutionary layer will feed on.

### 4.1 Risk gate does not validate SL/TP geometry (CRITICAL — falsifies PnL)

`risk/gate.py` checks stop *distance* but never stop *side*. Consequences in the live DB:

- `silver_basin_20260704_024202_RENDER` — short @ 1.6007 with "SL" at **1.4985 (below entry)**. Reconciliation's `find_first_cross` sees `high ≥ sl` on the first candle and closes at 1.4985 — a price that never traded — booking **+25.5% in 3 minutes**. Phantom.
- Same pattern: `..._SUI` (+7.6%), `..._022233_RENDER` (+4.1%) phantom wins; three wrong-side TPs closing longs at instant small losses. Also observed: `take_profit_price: 0.0` accepted.
- Six of 45 closed trades corrupted, all clustered in the leaderboard's #1 agent.

**Fix:** gate must reject unless (long: SL < entry < TP) / (short: TP < entry < SL), TP non-null and > fee hurdle, entry within ~0.5% of current heartbeat price (LLMs also hallucinate entry prices), and reward:risk sanity (e.g., ≥ 0.5). Then **void the corrupted trades** in the DB so the record is honest.

### 4.2 Decision attribution is confounded by the model chain (CRITICAL — breaks evolution)

The fallback chain means an agent's "genome" mutates randomly every tick depending on which free-tier endpoint answered. Free remote coding models ("Big Pickle," "North Mini Code Free") made most of the desk's trades. Beyond attribution, this is a reliability and privacy liability, and several agents show `last_model_used: "no model available"` — whole cycles silently skipped.

**Fix:** one pinned model per agent, recorded as part of agent config; fallback only *within* the same model (retry), never *across* models for trading decisions. The local Qwen server (12–20s/decision with reasoning off — good work) is the right default body; spend frontier-model budget (Claude) on the reflection loop where per-call value is 100× higher. If model identity is interesting, make it an explicit experiment: same thesis × two models = two agents.

### 4.3 Paper fills are systematically optimistic (HIGH — inflates every agent)

Fills execute at the heartbeat's last price — up to 5 minutes stale, no spread, no slippage — while the heartbeat *already computes* `spread` and `slippage_estimate` per asset. Round-trip taker fees (7 bps) are modeled, but crossing the spread twice plus impact is not. For strategies whose gross edge per trade is 30–80 bps, this alone can flip sign. Also: unrealized funding is not accrued on open positions, and `duration_minutes` is never written (45/45 closed trades NULL), so hold-time analysis is impossible.

**Fix:** fill at `price ± spread/2 ± slippage_estimate` by side; accrue funding on open positions in `update_position_pnl`; write duration on close. Paper must be *pessimistic* — the promotion decision depends on it.

### 4.4 Waits and skipped trades are not persisted (HIGH — selection bias in the memory)

The trade bank records only actions taken. The reflection loop will therefore learn from a censored sample — it can never discover "I skip too many good setups" or "my confidence is uncalibrated at 0.55–0.65" because the counterfactuals were discarded. Confidence *is* stored per trade (good), but there's no decisions table.

**Fix:** a `decisions` table logging every cycle: agent, timestamp, action (enter/wait/close/risk_blocked/error), reason, confidence, model, prompt hash, and top-N candidate assets considered. A nightly job fills in counterfactual outcomes for waits (what the entered-hypothetical would have returned at thesis-standard SL/TP). This turns every 5-minute cycle into training data instead of only the ~10% that trade.

### 4.5 Measurement is not decision-grade (MEDIUM)

- Sharpe is computed on per-trade leveraged `pnl_pct` — not time-weighted, not comparable across agents with different trade frequencies, and inflated by leverage choices. Compute daily equity-curve Sharpe (and Sortino), plus max-DD from the equity curve.
- `profit_factor` returns `inf` with zero losses (iron_moth, 2 trades) and sorts to the top of the leaderboard.
- Config drift: `config.yaml` says `starting_balance: 1000` while accounts were seeded at $50,000; `forge.py`'s fallback seeding uses a *different* set of agent names than `fresh_start.py`; duplicated `if __name__ == "__main__"` block at the bottom of `forge.py`.
- Effective exposure is opaque: `position_size_pct` (≤20%) × leverage (≤10×) allows 200% notional per position, 600% per agent across 3 slots. Cap *notional exposure* (size × leverage), not the two factors separately.
- No null model. Without a benchmark, "+0.4% in 3 days" is uninterpretable.

**Fix:** metrics rewrite + **two permanent benchmark agents**: `random_walk` (coin-flip entries, thesis-standard SL/TP and sizing, same risk gate) and `btc_hold`. Every leaderboard metric should be displayed *relative to the null distribution*. An agent is only "working" when it clears the random agent's 95th percentile over ≥100 trades.

---

## 5. Data Holes — What the Traders Can't See But Are Being Asked to Trade On

The heartbeat is impressive, but several theses reference evidence the system cannot supply. When the prompt promises data it doesn't deliver, the LLM either ignores its own thesis or hallucinates the value. Ranked by impact:

1. **No historical market data store — the foundational hole.** The proposal's "API-on-demand, no local store" decision must be reversed. Without stored history: no backtesting (kills the compiled-strategy paradigm), funding z-scores computed on 25h instead of the 14d the theses specify (silver_basin saw "z = +21.75" — a degenerate baseline, not a real 21σ event), OI baselines only as deep as the sampling file (100 samples), and pattern-persistence checks (anti-overfit rule #5) impossible. **Fix:** a `market_history` store (SQLite or parquet, separate from forge.db): 5m/1h/1d candles, hourly funding, OI snapshots, for the full universe; backfill from Hyperliquid's API (candles + funding history reach back far enough) and append from each heartbeat. Est. a few GB/year — trivial.
2. **Real liquidation data.** The current cascade *proxy* (OI drop + volume z + price move, all at 5-min resolution) is a blunt instrument for steel_crane's entire thesis. Hyperliquid's WS trade feed flags liquidations; Coinalyze/Coinglass offer cross-exchange liquidation + OI history cheaply. This is the single highest-value feed for the strategies most likely to have real edge (§6).
3. **Event calendar.** silver_basin's thesis has an "event calendar check" with nothing behind it. FOMC/CPI datetimes (static quarterly file), **token unlock schedules** (predictable forced supply — a real, documented edge in its own right), and exchange listing announcements. Cheap to add, referenced by theses today.
4. **Cross-exchange context.** Binance/Bybit funding + basis for the same assets: dislocations *between* venues are cleaner mean-reversion signals than absolute levels, and CEX data leads on-chain venues. One REST poll per heartbeat.
5. **Order book depth beyond top-5, sampled faster than 5 minutes — or kill the microstructure theses.** gray_finch is trading queue dynamics on data that is stale 60× over by the time the LLM answers. Either a WS book/tape sampler aggregating 1s imbalance into heartbeat features, or retire gray_finch/amber_wolf-style theses honestly. Recommendation: retire for now; microstructure at LLM latency is structurally unwinnable, and the slot is better spent on an event/unlock agent.
6. **Flow/positioning context (medium term):** BTC ETF net flows (daily, free), stablecoin market-cap delta, real BTC dominance (current value is OI-share within the 20-asset universe — mislabeled), long/short account ratios from CEXs.
7. **News/social (defer).** Highest noise, hardest to validate. Add only after the reflection loop exists to test whether it helps.

Also worth stating: **the data the system does have is close to sufficient for the strategies most likely to work.** Funding, OI, candles, and (proxied) liquidations are the raw material of every structural perp edge. The gap is history and events, not exotica.

---

## 6. Which Paradigms Are Most Likely to Make Money

Ranked by (structural persistence × fit to Forge's architecture × evidence in the literature and this DB):

1. **Funding harvest / funding dislocation (silver_basin's family).** The one edge in this market that is *paid, not predicted*: extreme funding is a cash flow you collect while positioned against a crowd that must eventually pay to stay. Self-refreshing (leverage demand never dies), measurable, and the desk's only coherent winners so far came from it (even after discounting phantoms). Extensions: delta-neutral funding capture (long spot/short perp — lower return, near-zero direction risk), cross-venue funding spreads.
2. **Liquidation cascade fade (steel_crane's family).** Forced flow is the most mechanical inefficiency in perps: liquidations are price-insensitive sellers/buyers, and the overshoot-revert pattern is well documented. Requires the real liquidation feed (§5.2). Pairs naturally with #1 (cascades reset funding).
3. **Event/unlock positioning (new agent).** Token unlocks and listings are scheduled, public, and repeatedly under-anticipated in mid-caps. Perfect for LLM reasoning (each event has idiosyncratic structure) at slow cadence with mechanical execution.
4. **Cross-sectional momentum / relative value at 4h–daily horizons (iron_moth, onyx_heron).** Real but crowded; survives fees only at longer horizons and modest turnover. Keep, but force holding periods ≥ 12h so the 5-min cadence stops shaking them out (the SL-inside-noise pattern in §2).
5. **Regime/vol transition trading (violet_lion, jade_hawk).** Legitimate as *filters and modulators* for the whole desk more than as standalone alpha. violet_lion's logic should probably migrate into the desk-level risk officer.
6. **Microstructure at 5-min LLM cadence (gray_finch, amber_wolf).** Structurally unviable as built. Retire or rebuild on streaming data much later.

Portfolio-level: the money-maker profile for Forge v1 is **a book that is mostly market-neutral-ish carry (funding) + episodic convexity (cascade fades, events), with momentum as the diversifier** — not 10 directional LLM day-traders. Daily-PnL-positive is a carry-book property, not a prediction-book property.

---

## 7. Target Architecture — The Three-Loop Desk

```
┌────────────────────────────────────────────────────────────────────┐
│ SLOW LOOP — Evolution (hours–days)  · frontier LLM (Claude)        │
│  reflection: trade bank + counterfactuals + backtests + research   │
│  → revised STRATEGY SPEC (versioned, validated, diffable)          │
│  meta-controller: evaluate vs null model · cull · spawn · graveyard│
└──────────────┬─────────────────────────────────────────────────────┘
               │ deploys spec (only after walk-forward validation)
┌──────────────▼─────────────────────────────────────────────────────┐
│ MID LOOP — Desk Risk Officer (30–60 min) · local LLM               │
│  regime synthesis, event awareness, gross-exposure throttle,       │
│  per-agent enable/disable, emergency posture. Cannot ADD risk.     │
└──────────────┬─────────────────────────────────────────────────────┘
               │ risk budget / kill flags
┌──────────────▼─────────────────────────────────────────────────────┐
│ FAST LOOP — Execution (5 min heartbeat, later WS) · pure Python    │
│  compiled agents: evaluate spec → orders                           │
│  LLM-control-arm agents: pinned model, temp 0, decision logging    │
│  risk gate (hardened) → paper/live bridge → fingerprints           │
└────────────────────────────────────────────────────────────────────┘
        ▲                                    │
        │   market_history store  ◄──────────┘ every heartbeat appends
        │   (candles, funding, OI, liqs, events — backtest substrate)
```

**The strategy spec is the pivotal new artifact.** Not free code (unsafe, unverifiable) and not a rigid config (kills expressiveness): a constrained JSON/YAML DSL over the heartbeat's feature vocabulary — entry conditions as weighted evidence terms (the theses already use exactly this shape!), exit rules, sizing curve, regime filters, max hold, per-asset scope. The existing thesis markdown remains the human-readable "why"; the spec is its executable shadow. The LLM writes both; the backtester referees; only validated specs deploy. Note how little distance there is between silver_basin's current thesis ("z > 2.0 → +0.7, acceleration match → +0.5, enter ≥ 0.70...") and a machine-executable spec — the theses were already converging on this form.

**What survives unchanged:** heartbeat, fingerprint store, bridges, risk gate (hardened), web UI, agent identity/lifecycle/graveyard, the persona framing (now aimed at reflection, where it belongs).

---

## 8. Development Path — Milestones and Tasks

The proposal's M6–M10 remain directionally right; this reorders and re-scopes them so that *truth precedes learning, and learning precedes capital*.

### M6 — Truth (1–2 weeks) — "the numbers mean what they say"
1. Harden `risk/gate.py`: SL/TP side + non-null TP + entry-price sanity vs heartbeat + R:R floor + notional-exposure cap (size×leverage); tests for each.
2. Void the six corrupted trades; rebuild affected account curves; annotate in DB (never delete — graveyard ethos).
3. Realistic paper fills: spread + slippage-estimate applied by side; funding accrual on open positions; write `duration_minutes`.
4. Pin one model per agent (config), retry-within-model only; log `model`, `temperature`, prompt hash on every decision.
5. `decisions` table + nightly counterfactual filler for waits.
6. Metrics rewrite: daily equity Sharpe/Sortino, capped PF, exposure-adjusted returns; leaderboard shows "vs null."
7. Seed `random_walk` + `btc_hold` benchmark agents.
8. Config hygiene: single source of truth for starting balance/universe; remove forge.py's divergent seed list and duplicated main block.

**Done when:** a week of runtime produces a leaderboard where every number is defensible, and the null agent's band is visible on it.

### M7 — Memory & the Backtest Engine (2–3 weeks) — the strategic unlock
1. `market_history` store + Hyperliquid backfill (≥ 12 months: 1h candles + funding; ≥ 90 days: 5m candles; OI/liq from Coinalyze or accumulate live).
2. Fix z-score windows to match thesis definitions (14d funding baseline etc.).
3. Event tables: funding settlements, macro calendar, **token unlocks**.
4. Strategy-spec DSL (schema + validator + interpreter over heartbeat features).
5. Backtester: replay history through the same interpreter + fee/slippage model as paper; walk-forward harness (train/validate/test windows); overfit metrics (deflated Sharpe, parameter-sensitivity sweep).
6. Hand-compile 3 seed specs from existing theses (silver_basin, steel_crane, iron_moth) and backtest them — this is also the first honest evidence about whether these theses have any historical edge at all.
7. Liquidation feed (WS flag or Coinalyze) replacing the proxy for steel_crane's family.

**Done when:** `backtest(spec, 2025-07→2026-06)` returns an equity curve + overfit report in under a minute, and the three seed specs have known historical profiles.

### M8 — Evolution (2–3 weeks) — the actual product
1. Reflection pipeline (frontier LLM): inputs = trade bank + decisions/counterfactuals + regime breakdown + backtest tools + (optional) web research; output = revised thesis + revised spec + self-declared invalidation conditions.
2. Anti-overfit gates as *code*, wrapping the proposal's rules 1–7 around the backtester: min-trades, holdout, cross-agent validation, throttle, pattern persistence, adversarial pass (second LLM call attacking the spec), regime flags.
3. Deploy pipeline: validated spec → versioned file + DB row → fast loop hot-reloads; full diff view in UI (proposal M6's UI tasks land here).
4. Convert the desk: 6–7 compiled agents + 2–3 pure-LLM control-arm agents (temp 0, pinned local model). Retire gray_finch/amber_wolf; spawn event/unlock agent.
5. Calibration report: per-agent confidence vs realized win-rate curves (data already exists in `confidence` column).

**Done when:** an agent completes reflection → spec revision → backtest validation → hot deploy with zero human touches, and a rejected overfit revision is visible in the reflection log.

### M9 — Selection (1–2 weeks)
1. Meta-controller evaluation job (proposal M7) with statistics that respect small samples: compare each agent to the null distribution, probation before termination, evaluation cadence in *trades* not days.
2. Cull/spawn/graveyard + harvest seeds; head-of-desk chat (frontier LLM with query tools over the bank) — this is also your daily briefing interface.
3. Desk risk officer (mid loop): hourly regime memo, gross-exposure throttle, event blackout windows (no new entries 2h pre-FOMC etc.), kill-switch authority. Constraint: it can only *reduce* risk, never add.
4. Diversity maintenance: spawn thesis-similarity check (now real, via embedding or LLM compare against graveyard).

**Done when:** the desk runs 2+ weeks unattended: evaluated, culled, spawned, throttled — with a coherent morning summary in the chat.

### M10 — Live, Small (2–3 weeks)
1. Live-bridge hardening: exchange-native trigger orders for SL/TP (never rely on the 5-min local loop for real stops), asset-specific size/price decimals (szDecimals — the current `round(size,4)` will get orders rejected), partial-fill and IOC-miss handling, position reconciliation against exchange state on every heartbeat, fix the broken sync/async mix in `get_positions`.
2. Shadow mode (proposal M9 task 2): paper + live simultaneously, slippage report.
3. Promotion gate: ≥ 100 paper trades, beats null at 95%, positive after modeled costs, calibrated confidence, human click. Start with $500–1,000 on ONE agent (likely a funding-family agent).
4. Live audit log, webhook alerts, live emergency stop; daily automated paper-vs-live divergence report.

### M11 — Compounding Operations (ongoing)
1. Capital allocation across live agents ∝ shrunk Sharpe (fractional-Kelly capped); weekly rebalance by meta-controller, human-approved.
2. Ops hardening from proposal M8/M10: settings UI completion, restart recovery tests, DB backups, Docker, health metrics.
3. Research flywheel: monthly "strategy review" reflection at desk level — retire crowded edges, propose new families (this is where LLM creativity compounds into robustness-over-time).

---

## 9. Honest Expectations & Principal Risks

- **Calibrate the targets.** The proposal's 25–45%/agent with Sharpe > 1.5 is aspirational; a realistic v1 success is: *funding-carry book yielding 10–25% annualized on small capital with max DD < 10%, plus optionality from event/cascade agents* — and, more importantly, a machine that produces and validates new strategies faster than old ones decay. That second thing is the actual asset being built.
- **Capacity is real but fine at this scale.** These edges hold at $10k–$1M; they will not hold at $100M. That is not a problem worth solving yet.
- **Biggest technical risk:** overfitting via the reflection loop — the system optimizing its specs into historical noise. Mitigation is M7's walk-forward + deflated-Sharpe machinery and the null-agent floor; treat the anti-overfit gates as the most important code in the repo.
- **Biggest operational risk:** live stops living in a 5-minute local loop. Exchange-native triggers are non-negotiable before real money (M10.1).
- **Biggest strategic risk:** spending the next quarter polishing the fast loop (more features, more agents, more UI) instead of building M7–M8. The fast loop is done enough. The moat is the slow loop.
- **Regulatory:** unchanged from the proposal — non-custodial DeFi, US person, consult counsel before scaling live capital.

---

## 10. Summary of the Strategy in Five Sentences

Forge's ecosystem premise is right; its current LLM placement is one level too low. Fix measurement integrity first — today's results are contaminated by a risk-gate hole and randomized model attribution, and no learning can happen on corrupted signal. Reverse the no-history decision: a market-history store plus a backtest engine converts evolution from months-per-generation to minutes-per-generation, and it is the single highest-leverage build. Rebuild agents as LLM-authored, mechanically-executed, backtest-validated strategy specs — keeping a pure-LLM control arm so the arena itself answers the paradigm question. Concentrate the book on structural perp edges (funding, liquidations, events) where the market pays you for a mechanism rather than a prediction, and go live only through the null-model gauntlet, small.
