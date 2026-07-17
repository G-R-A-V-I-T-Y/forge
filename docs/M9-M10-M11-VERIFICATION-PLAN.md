# M9/M10/M11 Live Verification Plan

**Status:** All code implemented and committed. Awaiting 2+ week runtime for operational verification.

**Date:** 2026-07-16  
**System:** Forge paper trading on Hyperliquid  
**Branch:** main (commit 1376ef9)

---

## Verification Prerequisites

### System Configuration
- ✅ 9 trading agents (rookie status)
- ✅ 2 benchmark agents (random_walk, btc_hold)
- ✅ Hyperliquid data source configured
- ✅ 5-minute heartbeat cycle
- ✅ All scheduled jobs configured:
  - Reflection scheduler (30 min check)
  - Evaluation controller (30 min check)
  - Risk officer (30 min)
  - Challenger resolution (60 min)
  - Labeling job (nightly 03:00 UTC)
  - Head of desk briefing (daily 06:00 UTC)
  - Ledger compaction (monthly)

### Data Collection Started
Run: `python forge.py` to begin accumulating:
- Trade fingerprints
- Decision records (enter/wait/close)
- Ledger streams (candles, funding, OI, liquidations)
- Evaluation cycles
- Reflection cycles

---

## M9 Verification Checklist (2+ weeks)

### M9.14: Selection & Daily Improvement Loop

**Acceptance Criteria:**

1. **Reflections fire on cadence** ✓
   - [ ] At least 3 agents reach 20 trades
   - [ ] Reflection cycles appear in reflections table
   - [ ] Spec revisions deployed as challenger
   - [ ] Verify in: `SELECT * FROM reflections WHERE outcome IS NOT NULL`

2. **Spec revision decided on merits** ✓
   - [ ] Walk-forward validation runs (check backtest_trials table)
   - [ ] At least 1 spec passes walk-forward and deploys
   - [ ] At least 1 spec fails walk-forward and is rejected
   - [ ] Verify rejection_reason contains walk-forward failure details

3. **Evaluations run on trade cadence** ✓
   - [ ] Evaluations table has entries every ~30 trades per agent
   - [ ] Significance test against benchmark_random_walk runs
   - [ ] p-values recorded in evaluations table
   - [ ] Query: `SELECT agent_id, COUNT(*) FROM evaluations GROUP BY agent_id`

4. **Underperformer lifecycle** ✓
   - [ ] At least 1 agent crosses PF<0.8 threshold → SUSPENDED
   - [ ] At least 1 agent crosses WR<35% after 50 trades → TERMINATED
   - [ ] Seeds harvested on termination (check seeds table)
   - [ ] Query: `SELECT id, status, termination_reason FROM agents WHERE status IN ('suspended', 'terminated')`

**Verification Commands:**
```sql
-- Check reflection cycles
SELECT agent_id, triggered_at, outcome, rejection_reason 
FROM reflections 
ORDER BY triggered_at DESC LIMIT 20;

-- Check evaluation cadence
SELECT agent_id, evaluated_at, decision, p_value, pf_consecutive_low 
FROM evaluations 
WHERE decision != 'continue' 
ORDER BY evaluated_at DESC;

-- Check seeds harvested
SELECT agent_id, pnl_pct, thesis_excerpt 
FROM seeds 
WHERE used = 0 
ORDER BY pnl_pct DESC;
```

---

## M10 Verification Checklist (≥14 days ledger)

### M10.13: Honest Reflection Engine

**Acceptance Criteria:**

1. **Scheduled reflection builds dossier** ✓
   - [ ] Dossier includes forward-labeled decisions
   - [ ] High-regret decisions visible in reflection prompt
   - [ ] Calibration curve computed from decisions table
   - [ ] Check: reflection row has research_findings_json populated

2. **Hypothesis registration** ✓
   - [ ] Stage A (Diagnose) creates hypotheses rows
   - [ ] Hypotheses have status='proposed' initially
   - [ ] predicted_effect and falsification_condition recorded
   - [ ] Query: `SELECT * FROM hypotheses WHERE status='proposed'`

3. **Walk-forward decides deployment** ✓
   - [ ] Mandatory walk-forward runs (no silent skips)
   - [ ] Deflated Sharpe computed from backtest_trials count
   - [ ] Specs with deflated_sharpe > 0 deploy as challenger
   - [ ] Specs with deflated_sharpe ≤ 0 rejected

4. **Challenger auto-resolves** ✓
   - [ ] Challenger specs log decisions without executing
   - [ ] After 20 labeled decisions or 7 days, resolution runs
   - [ ] Winner promoted to active, loser rejected
   - [ ] Hypotheses updated to validated/falsified
   - [ ] Query: `SELECT spec_version, status FROM specs WHERE status IN ('challenger', 'rejected')`

**Verification Commands:**
```sql
-- Check forward-labeled decisions
SELECT decision_id, horizon, regret_pct, best_action 
FROM decision_labels 
WHERE regret_pct > 0.02 
ORDER BY regret_pct DESC LIMIT 10;

-- Check hypothesis lifecycle
SELECT agent_id, claim, status, predicted_effect, effect_observed 
FROM hypotheses 
WHERE status IN ('validated', 'falsified') 
ORDER BY resolved_at DESC;

-- Check challenger trials
SELECT agent_id, spec_version, status, deployed_at 
FROM specs 
WHERE status IN ('challenger', 'active', 'rejected') 
ORDER BY deployed_at DESC;
```

---

## M11 Verification Checklist (2+ weeks)

### M11.13: Population Learning & Ecosystem Honesty

**Acceptance Criteria:**

1. **Falsified hypothesis blocks spawn** ✓
   - [ ] At least 1 hypothesis reaches status='falsified'
   - [ ] Spawn attempt with matching (feature, direction, regime) rejected
   - [ ] Rejection cites specific hypothesis registry row
   - [ ] Check spawner logs for graveyard rejection

2. **Crossover child trades** ✓
   - [ ] At least 2 agents terminated with seeds harvested
   - [ ] spawn_from_seeds called with 2+ seed_ids
   - [ ] LLM synthesizes thesis from parent excerpts
   - [ ] Walk-forward validates spec before first trade
   - [ ] New agent created with spawned_agent_id links in seeds table

3. **Immigration quota holds** ✓
   - [ ] Track last 3 spawns
   - [ ] If all 3 are crossover → next must be fresh
   - [ ] Verify spawn_source diversity over 10 spawns
   - [ ] Query: `SELECT id, config_json FROM agents WHERE created_at > ? ORDER BY created_at`

4. **Bootstrap p-values drive culls** ✓
   - [ ] Evaluations use bootstrap test (not normal approximation)
   - [ ] 1000 resamples from benchmark_random_walk
   - [ ] p-value < 0.05 after 100 trades → cull decision
   - [ ] Verify in evaluations table: p_value column populated

**Verification Commands:**
```sql
-- Check desk memory digest
SELECT agent_id, status, effect_observed 
FROM hypotheses 
WHERE status IN ('validated', 'falsified') 
ORDER BY effect_observed DESC;

-- Check trial accounting
SELECT agent_id, COUNT(*) as trial_count, AVG(deflated_sharpe) 
FROM backtest_trials 
GROUP BY agent_id;

-- Check spawn history
SELECT id, created_at, config_json 
FROM agents 
WHERE created_at > datetime('now', '-7 days') 
ORDER BY created_at;
```

---

## Monitoring Dashboard

**Navigate to:** http://localhost:8000

**Key Metrics to Watch:**

### Overview Page
- Desk equity trend
- Null band visualization
- Desk deflated Sharpe (should decrease as trials accumulate)
- Hypothesis validation rate
- Diversity score
- Active agent count

### Agent Detail Pages
- Dossier digest tab (high-regret decisions)
- Hypotheses tab (status transitions)
- Reflection cycles tab (outcomes)
- Calibration tab (confidence vs win rate)

### Decisions Page
- Labeling coverage (should reach >90% for decisions >24h old)
- Regret distribution

---

## Timeline

| Week | Expected Events |
|------|----------------|
| Week 1 | First 20-trade reflections, evaluation cycles, some suspensions |
| Week 2 | Challenger trials resolve, first hypothesis validations/falsifications, ≥14 days ledger accumulated |
| Week 3 | First terminations → seeds harvested, crossover spawning begins |
| Week 4+ | Population learning visible, immigration quota enforced, desk memory propagates |

---

## Success Criteria

**M9 Complete When:**
- ≥3 reflection cycles completed with outcomes
- ≥1 agent suspended or terminated with seeds harvested
- Evaluations running every 30 trades for all agents

**M10 Complete When:**
- ≥1 dossier-driven reflection with forward-labeled regret
- ≥1 challenger trial resolved (promoted or rejected)
- ≥1 hypothesis validated or falsified

**M11 Complete When:**
- ≥1 falsified hypothesis blocks a spawn
- ≥1 crossover child agent created and trading
- Immigration quota verified over 3+ spawns
- Bootstrap p-values used in ≥5 evaluation cycles

---

## Next Steps

1. **Start the system:** `python forge.py`
2. **Monitor daily:** Check http://localhost:8000 and review logs
3. **Verify weekly:** Run SQL queries above to track progress
4. **Document results:** Update this file with actual timestamps and outcomes
5. **After 2 weeks:** Review all acceptance criteria and mark complete

**Estimated Completion:** 2026-08-02 (2+ weeks from 2026-07-16)
