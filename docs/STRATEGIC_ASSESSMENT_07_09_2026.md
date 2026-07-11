# Strategic Assessment — 2026-07-09

**Scope:** Comprehensive code and project review of Forge against `docs/FORGE_PROPOSAL.md`, testing the claim that the project is complete and consistent with the proposal through M10.

**Method:** Full read of the proposal; line-level review of `forge.py`, `meta/*`, `agents/*`, `backtest/*`, `risk/*`, `execution/*`, `store/*`, `web/app.py`; inspection of the live `data/forge.db` and the July ledger partitions; full test-suite run (483 passed under Anaconda Python with the two documented apscheduler ignores).

---

## 1. Verdict

**The claim "complete through M10" is not true.** M1–M7b are substantially real. M8's *components* are real but its roster conversion has been silently lost from the running system. M9 exists as a set of modules that are miswired, misconfigured, and in several places actively harmful. M10 is roughly half-delivered (design system: yes; executive-action safety, missing pages, settings: no).

More important than any milestone checklist: **the desk cannot trade today.** The live ledger and database prove it:

- Every LLM-routed agent logs `"wait — LLM returned invalid response after retries", model: null` on every cycle. The cause is a one-line signature mismatch (see C1). **No pure-LLM agent has made a single successful decision under the current wiring.**
- The current `forge.db` contains **zero trades, zero evaluations, zero reflections, zero seeds, no benchmark agents**, and only one compiled agent (`sage_turtle`), which has only ever logged waits.
- The nightly counterfactual job crashes on an undefined variable — and even if it didn't, it asks an LLM to *hallucinate* trade outcomes instead of replaying recorded candles.
- The risk officer writes entry-disable rows to a table **nothing in the decision path reads**. The UI's "Disable Entries" button is likewise decorative.

Meanwhile **483 unit tests pass.** That is the central process lesson of this review: the tests validate modules in isolation with correctly-shaped mocks, while every critical defect lives in the *composition* — `forge.py`'s wiring, config-key contracts between modules, and the difference between "a function exists" and "production calls it." Nothing tests the assembled system. Until an end-to-end smoke test exists (R10), "N tests passing" carries no information about whether Forge works.

The honest framing for the owner: **the foundation (ledger, DSL, backtester, risk gate, heartbeat/features) is genuinely good — better than typical for a project at this stage. The evolutionary superstructure built on top of it in M9/M10 was assembled without integration discipline and has never actually run.** The moat the proposal describes ("a machine that produces and validates strategies faster than they decay") compounds only under load, and the machine has never been under load.

---

## 2. What is genuinely good (keep, don't churn)

- **`risk/gate.py`** — 13 real, hard rules; stateless; no I/O; well tested. The M6 hardening happened.
- **`backtest/`** — the DSL, validator, interpreter, engine, and walk-forward harness are careful work. Lookahead discipline is real (per-bar bisect cutoffs, funding window capped to live's 14-day lookback, events visible only if `scheduled_time >= bar_ts`). Parity reasoning is documented in-code. The deflated-Sharpe penalty is simplified but honest about it.
- **Git-native ledger** (`store/ledger.py`, `git_sync.py`, `compact_ledger.py`, `state_snapshot.py`) — the append-only JSONL → Parquet design is implemented as specced and the "wait decisions carry confidence/evidence" discipline is in the writer's contract.
- **`market/heartbeat.py` / `features.py`** — a single `compute_replayable_fields` shared by live and backtest is exactly the right architecture; the backtest engine consuming `candles_5m` for parity (with the documented reasons) is right.
- **The seed-backtest report culture** — `2026-07-07-seed-backtest-results.md` reporting "no proven edge yet" is the system being honest. Protect that culture.
- **Design tokens** (`web/static/forge.css`) — M10 AC#1's token-based dark-first system substantively exists.

---

## 3. Critical defects (the desk is down)

Ordered by severity. File:line references are to the current working tree.

### C1. Fleet LLM calls fail on a signature mismatch — no LLM agent can decide
`agents/decision_loop.py:409` calls `llm_fn(system_prompt, decision_prompt, agent_id=agent_id)`, but the production closure in `agents/agent_runner.py:78` is `def llm_fn(system_prompt, decision_prompt)`. Every call raises `TypeError`, which `_call_llm_with_retry` swallows and retries, then logs *"LLM returned invalid response after retries"*. The July ledger confirms this is happening on every cycle for every non-compiled agent. Secondary defect in the same closure: it never forwards `agent_id` to `model_chain.decide()`, so per-agent **pinned models** (a proposal pillar; `llm/model_chain.py:456-471` supports it) have never been applied in production.

### C2. The current roster is not the M8 roster — and a DB wipe silently un-compiles the desk
`iron_moth`, `silver_basin`, `jade_hawk` have `config_json = {}` in the live DB — not compiled, no deployed specs (the `specs` table has one row: sage_turtle). Root causes:
- **Two divergent seeding paths.** `forge.py:246-295` has its own minimal seed list (10 names, empty configs, plus sage_turtle special-casing); `scripts/fresh_start.py` is the real one (compiled flags + `deploy_spec` for the 3 seed specs). M6 task 8 explicitly removed forge.py's divergent seed list; it has regressed back in.
- **Recovery loses roster state.** `state/current.json` doesn't carry `config_json`, and `scripts/rebuild_local_cache.py` doesn't restore compiled flags or `specs`-table rows. The specs *YAML files* are git-tracked, but `get_active_spec` requires a DB row. Burned-laptop recovery (the M7a headline guarantee) restores balances but demotes every compiled agent to a broken pure-LLM agent.

### C3. No benchmark agents exist — and their absence arms a kill-everything bug
`scripts/seed_benchmarks.py` exists but neither `forge.py` nor anything scheduled runs it; the DB has no `benchmark_random_walk` / `benchmark_btc_hold`. Consequences:
- Every leaderboard "vs null" claim is empty; M9's evaluation math has no null distribution.
- **`meta/evaluator.py:215-223`: with `null_metrics = None`, `beats_null` is `False`, so *any* agent reaching 100 closed trades is terminated regardless of performance.** The meta-controller job runs every 30 minutes. Once trading resumes, the desk terminates itself at trade 100.
- Even if seeded, `btc_hold` can never enter: its decision uses `position_size_pct: 0.50` (`agents/decision_loop.py:149`), which risk-gate Rule 8 caps at 0.20. The benchmark is dead on arrival.

### C4. The counterfactual pipeline is both crashed and conceptually wrong
- `forge.py:371` — the nightly job's lambda references `llm_fn`, which is **undefined** in `forge.py`. `NameError` every night at 02:00 UTC.
- `agents/decision_loop.py:558-619` — even if called, `run_counterfactual` analyzes only the **single most recent** wait per agent (design: all unfilled waits), joins it to an arbitrary unrelated trade via `LEFT JOIN trades ON d.agent_id = t.agent_id`, and then **asks an LLM to guess whether the trade would have won** — with no future price data in the prompt. `counterfactual_was_better` is set to whether the LLM said "long/short", not whether the hypothetical made money.
- The proposal's design is deterministic: replay the wait against subsequent `candles_5m` at thesis-standard SL/TP. All the data needed is already in the ledger. There is no reason for an LLM to be in this loop at all.
- Downstream: calibration reports, the pattern-persistence gate, and the reflection loop's "counterfactuals: N would have been better" prompt line are all consuming an empty or hallucinated signal. Current DB: 79 decisions, 0 counterfactuals.

### C5. Compiled agents destroy their own calibration data
`agents/decision_loop.py:211-219` — when a compiled agent waits, it logs `confidence=0.0` (the code path never tracks the best sub-threshold confidence; `best_confidence` there is the *threshold*, not an observation). The entire M7a selection-bias fix — "a wait carries the same structured confidence as an enter" — is nullified for exactly the agents (compiled) that are supposed to be the desk's future. sage_turtle's every ledger wait reads `confidence: 0.0, evidence_strength: {}`.

### C6. The risk officer is unconsumed, misconfigured, and a one-way ratchet
`meta/risk_officer.py`:
- **Unconsumed:** nothing in `decision_loop`/`agent_runner`/bridges checks `entry_disables` or `is_entry_gate_open` (grep confirms: writers only). Neither the officer nor the human UI toggle affects trading.
- **Misconfigured:** it reads top-level config keys that don't exist in `config.yaml` (`max_position_size` → default **$1,000 notional** vs. real $5–10k positions; `agent_daily_loss` → $100 ≈ 0.2% of one account; `daily_loss_limit` → $500 desk-wide across ~$450k of paper equity; `drawdown_kill_pct` → ignores `desk.drawdown_kill_pct: 0.15`, and if it *did* read it, would divide by 100 into 0.15%). If ever consumed as-is, these defaults would freeze the desk within hours.
- **Ratchet:** `run_cycle` inserts a new disable row every 5 minutes for any closed gate, and nothing automatic ever calls `enable_entry` — a transient breach becomes a permanent, row-spamming lockout.
- **vs. proposal AC#5:** no regime memo, no gross-exposure throttle vs 2× equity, no event-calendar blackout, no reduce-only validator. None of the four specified risk-officer tests exist.
- Also: daily PnL is grouped by `DATE(entry_timestamp)`; realized PnL belongs to the exit date.

### C7. `meta/head_of_desk.py` is the wrong module — and it has already polluted the desk
The proposal's Head of Desk is a daily briefing + a chat interface over the full trade bank (M9 AC#6). What shipped is an auto-spawner that keeps the roster at `config.get("target_agent_count", 5)` — read from the **top level** of config (the real key is `desk.target_agent_count: 10`), so it targets 5 — by minting agents from five hardcoded generic archetypes ("Momentum Trader", "Scalper" — a paradigm the proposal explicitly retired as structurally unviable) with three-line junk theses and collision-prone names (`agent_momentum_1`). It bypasses `check_against_graveyard` and the `seeds` table entirely. The orphaned `agents/theses/agent_mean_reversion_2_v1.md` / `agent_momentum_1_v1.md` files in the working tree prove it has already fired. This is selection pressure in reverse: cull the carefully-designed cohort, replace with noise.

### C8. The reflection loop cannot do its job as wired
- **LLM contract mismatch:** `forge.py:529-537` builds the reflection `llm_fn` from `model_chain.decide()` — a *trading-decision* API that JSON-parses its output against the decision schema — then `json.dumps()`s the result. The reflection prompt asks for spec **YAML**. What comes back is a JSON decision dict, which `yaml.safe_load` happily parses (JSON ⊂ YAML) and `_dict_to_spec` converts into an **all-defaults spec with zero evidence terms**.
- **For agents with no current spec (all control-arm/pure-LLM agents), the hollow gates then wave it through:** holdout is skipped when `current_spec is None` (`agents/reflection.py:204`), cross-agent and pattern-persistence are skipped when `evidence` is empty (`:220,:241`), walk-forward is skipped because `config["ledger_dir"]` is never set (`:258`) — and `deploy_spec` is called with `config.get("desk_config")` = `None` (the real key is `desk`), which **skips spec validation** (`store/specs.py:118-119`). Net effect: the scheduler's first eligible pass on a control-arm agent deploys an unvalidated, evidence-free, long-only default spec.
- **The gates themselves are Potemkin** (proposal: "the most important code in the repo"):
  - `check_holdout_split` never evaluates the revised spec on held-out trades — it checks `len(trades) >= 30` and non-empty evidence. It cannot reject an overfit revision.
  - `check_cross_agent_validation` returns `(True, None)` on every path.
  - `check_pattern_persistence` checks only that the agent's trade *history spans* 3 weeks — nothing about the feature or condition.
  - The walk-forward gate — the only real one — is optional and unreachable in production (no `ledger_dir`).
- **No reflection log:** nothing in production inserts into `reflections` (grep: only a test does). The scheduler's eligibility check reads `reflections.triggered_at` (so agents stay "eligible" forever) and its outcome `UPDATE` targets rows that don't exist. M9 AC#1's "reflection log queryable per agent" is unimplemented.
- Reflection model choice contradicts the proposal (frontier LLM for reflection); as wired it goes to the same free-tier opencode/local chain as decisions.

### C9. Executive actions have sharp edges (M10)
- **Emergency stop** (`web/app.py:785-792`): `UPDATE agents SET status='suspended'` **with no WHERE clause** — it resurrects `terminated`/`culled` agents into `suspended`, corrupting the permanent graveyard — and it **closes zero positions** (proposal: "closes all open positions immediately"). It currently does the opposite of both halves of its job.
- **Promote-to-Shadow / Go-Live are placebo-harmful:** the fleet only runs `status IN ('rookie','active')` (`forge.py:202`), so promoting an agent to `shadow` or `live` **stops it trading entirely**. No shadow dual-bridge or LiveBridge routing exists (that's M11 — fine), but the buttons exist now and silently kill the agent.
- **Reflect-Now** requires `app.state.llm_fn`, which `forge.py` never sets → always HTTP 503 in production.
- **No confirmation token** anywhere (`grep -c confirm web/app.py` → 0); proposal AC#6 and its test (`test_actions_require_post_and_confirm`) require one. Manual position close writes no audit row.
- Missing pages: `/graveyard`, `/decisions`, `/chat` routes don't exist at all. `/api/settings` handles llama-server settings only — none of AC#5's desk settings (wake cadence, evaluation thresholds, universe, risk caps).

---

## 4. Measurement-integrity defects (paper numbers are inflated)

These matter because every promotion/cull/reflection decision keys off paper P&L. Current bias is systematically **optimistic** — the opposite of the proposal's "paper is pessimistic" principle.

| # | Defect | Where | Effect |
|---|---|---|---|
| M1 | Fees charged on **margin**, not leveraged notional (`notional_usd = balance × size_pct`, no `× leverage`) | `execution/paper_bridge.py:83`, `store/positions.py:143-144` | Fees understated by the leverage factor (3–10×). The backtester charges `2 × taker_fee × leverage` — correct — so live-paper and backtest disagree on every trade. For strategies near the fee hurdle this flips signs. |
| M2 | Funding accrual also computed on margin-sized position | `store/positions.py:74` | Funding flows off by the leverage factor — fatal to measuring `silver_basin`-class funding strategies. |
| M3 | SL/TP misses intra-candle wicks: reconcile scans candles only when the *current heartbeat price* is outside bounds | `store/positions.py:364-383` | A stop wicked through and recovered within 5 min is never triggered → phantom survivals, some later booked as wins. |
| M4 | `max_hold_hours` never enforced live | `store/positions.py` (absent) | Backtest exits on time-stop; live positions ride to SL/TP forever. sage_turtle's "exit event+24h" is unimplemented live. |
| M5 | Backtest models **no funding PnL** at all | `backtest/engine.py:231-259` | The #1 paradigm in the proposal (funding harvest) cannot show its edge in the machinery built to validate it. |
| M6 | Live scales size by confidence *after* the risk gate validated the unscaled order; backtest ignores scaling entirely | `agents/decision_loop.py:326-328` | Known M7b limitation, still open; gate validates a different order than executes. |
| M7 | Live compiled agents can stack multiple positions in the same asset (no per-asset guard); backtest holds max one per asset | `agents/decision_loop.py` vs `backtest/engine.py:149` | Parity break + unintended pyramiding up to the 3-position cap. |
| M8 | Trade IDs have 1-second resolution → PK collision if same agent+asset entered twice in a second | `execution/paper_bridge.py:17-20` | Low probability, corrupts a trade row when it hits. |

---

## 5. Milestone scorecard

| Milestone | Claimed | Reality |
|---|---|---|
| M1–M5 | DONE | Substantially real. |
| M6 Truth | DONE | Gate/metrics/decisions real. **Regressions:** benchmarks not seeded (C3), divergent seed list back in forge.py (C2), model pinning inert (C1), counterfactual filler wrong+crashed (C4). |
| M7a Ledger | DONE | Real and good. Caveat: recovery loses compiled-roster state (C2b). |
| M7b DSL/Backtest | DONE | Real and good. Known limits honestly documented; funding-PnL gap (M5) under-flagged. |
| M8 Evolution | DONE | Components real (deploy pipeline, interpreter path, diff/calibration UI, graveyard similarity fn). **But:** roster conversion lost in prod (C2), gates hollow (C8), `check_against_graveyard` has zero production callers, reflection never yet run end-to-end (its "done when" was explicitly deferred to M9 — and M9 didn't close it). |
| M9 Selection | Implied done | **Not done.** Scheduler jobs exist and fire, but: reflection can't produce valid specs (C8), evaluation kills everything at 100 trades absent benchmarks (C3), risk officer unconsumed/misconfigured (C6), Head of Desk is the wrong module (C7), no briefing, no chat, no counterfactual coverage surfacing, harvest-`seeds` written but never read, proposal test files (`test_reflection_schedule.py`, `test_meta_controller.py`, `test_head_of_desk.py`, `test_risk_officer.py`) don't exist. |
| M10 Command Deck | Implied done | **Half.** Token design system: yes. Exec endpoints + audit_log: exist. Missing: confirm tokens, safe emergency stop, 3 of 8 pages, desk-settings roundtrip, `llm_fn` wiring; shadow/live buttons actively harmful (C9). |

---

## 6. Strategic assessment — against the goal of making money

1. **The bottleneck is not strategy quality; it is that the machine has never run honestly.** The proposal's own "biggest strategic risk" — *admiring the machinery instead of running it* — has materialized in a worse form: machinery that *looks* like it's running (jobs scheduled, tests green, dashboards up) while producing nothing. Before any new capability: make the fast loop actually trade, make paper numbers mean what they say, and let the desk run for 2–4 uninterrupted weeks.

2. **Simplicity should be enforced by deletion, not addition.** The M9 modules that diverge from the proposal (archetype auto-spawner, decorative entry gates, LLM counterfactuals) are not half-finished versions of the right thing — they are the wrong thing. Deleting them is progress. The system the proposal describes is *smaller* than what exists: deterministic replay for counterfactuals, a reduce-only throttle, one seeding path, one place that spawns.

3. **Protect the calibration channel above almost everything.** Confidence-on-every-decision is the system's unique data asset — it's what makes reflection more than curve-fitting. Right now the channel is severed at three points (C1 nulls, C4 empty counterfactuals, C5 zeroed compiled waits). All three fixes are small.

4. **The control-arm-vs-compiled experiment is currently unfalsifiable.** The control arm can't decide (C1) and mostly isn't pinned to any model; the compiled arm is one agent. Fixing C1/C2 and pinning control-arm agents to the local llama-server (temp 0, per proposal) is what makes the "does LLM reasoning beat compiled specs?" question answerable. Reserve the opencode free-tier chain for reflection experiments, not decisions — decision-time model roulette destroys attribution.

5. **Funding-harvest needs the backtester to see funding.** The proposal ranks funding harvest #1 precisely because it's *paid, not predicted* — but the backtest engine can't measure payment (M5). Until funding PnL is in the engine, walk-forward reports on `silver_basin`-class specs are structurally biased against the desk's best paradigm.

6. **Data discipline is holding.** Ledger streams are lean and structured; decisions carry evidence (where not nulled by C1/C5); compaction is scheduled and load-bearing. The one drift: forge.py startup rewrites committed files (`agents/theses/sage_turtle_v1.md` was silently replaced by a shorter embedded copy; `deploy_spec` reformats `sage_turtle_v1.yaml` on every boot), leaving the repo permanently dirty since `git_sync` only commits `ledger/` + `state/`.

7. **Process: adopt "wiring tests or it didn't happen."** Every critical defect here would have been caught by one integration test that boots the real composition with a stub market and stub LLM and asserts a trade happens. The proposal's per-milestone test tables were partially replaced by module-level tests that mirror the implementation rather than the acceptance criteria. For agentic execution specifically, acceptance tests must be written against the *proposal's contract*, not against the code as built.

---

## 7. Revisions

Ordered by dependency and value. R1–R4 are "the desk turns on and the numbers are honest." R5–R8 are "M9 becomes real." R9 and R11 are consolidation. R12 is a lightweight, config-scale gate that must land immediately before any unattended run starts. Each is scoped to be executable by an agentic coder without further context.

**Addendum (2026-07-10):** the owner asked what is minimally required before starting an uninterrupted two-week paper-trading run. The answer is R1, R2, R4, R5, the capture half of R3 (full replay can land mid-run), a trimmed R10, and R12 below — see the revised §8 sequencing. R6, R7, the rest of R8, R9, and R11 are explicitly *not* required to start the run and should not delay it.

---

### Revision R1 — Restore the fast loop: fix the fleet LLM contract and pinned models
**Goal:** Every non-compiled agent completes real LLM decision cycles again; pinned per-agent models are honored; the nightly counterfactual job no longer crashes on an undefined name.

**Acceptance Criteria**
1. `agents/agent_runner.py`'s `llm_fn` accepts and forwards `agent_id` (signature-compatible with `decision_loop._call_llm_with_retry`'s `llm_fn(system_prompt, decision_prompt, agent_id=...)`), and passes it to `model_chain.decide(..., agent_id=agent_id)` so pinned models resolve.
2. `forge.py`'s `_run_counterfactual_job` no longer references the undefined `llm_fn` (see R3 — the job becomes deterministic; if R3 is deferred, the job must at minimum be wired to a defined callable and tested).
3. A ledger `decisions` line from a live cycle of a pure-LLM agent shows a non-null `model` and a real reason (not "invalid response after retries").
4. `run_postmortem`'s fire-and-forget `asyncio.ensure_future` is awaited (or explicitly gathered) before `agent_runner` exits, so postmortems aren't dropped when the subprocess terminates.

**Dependencies:** none. **Do this first.**

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r1-llm-contract` | agent_runner signature + agent_id pass-through + postmortem await |

**Tests**
- `tests/test_agent_runner_contract.py::test_llm_fn_accepts_agent_id_kwarg` — construct the actual closure from `agent_runner` and call it exactly as `_call_llm_with_retry` does (kwarg included); no `TypeError`.
- `tests/test_agent_runner_contract.py::test_pinned_model_forwarded` — with an agent whose config pins a model, assert `model_chain.decide` receives `agent_id`.
- Integration (see R10): fleet cycle with stub chain yields non-null `model` in the decisions ledger.

---

### Revision R2 — One seeding path; roster and benchmarks survive restarts and rebuilds
**Goal:** The M8 roster (4 compiled agents with deployed specs, control arm, benchmarks) exists after any of: fresh start, forge.py boot on an existing DB, or disaster-recovery rebuild. forge.py stops carrying its own divergent seed list.

**Acceptance Criteria**
1. `forge.py` contains no inline seed roster and no inline thesis text; on an empty DB it delegates to the same seeding code `scripts/fresh_start.py` uses (single source of truth).
2. On every startup, forge.py reconciles compiled agents: for each agent with `compiled: true` (or with a committed spec file `agents/specs/{id}_v*.yaml`) but no active `specs` row, the latest spec file is deployed. sage_turtle's special-casing generalizes to all compiled agents.
3. `benchmark_random_walk` and `benchmark_btc_hold` are seeded (idempotently) at startup, run in the fleet cycle, and are **excluded** from meta-controller lifecycle decisions and from head-of-desk population counts.
4. `btc_hold` establishes its position legally: either its order conforms to the risk gate (e.g. size ≤ `max_position_size_pct`, entering once and holding) or benchmarks get a documented, code-enforced exemption path — pick the former.
5. `state/current.json` (or the rebuild script reading `agents/specs/`) restores `config_json` compiled flags and active-spec state so `rebuild_local_cache.py` → `forge.py` yields the same roster behavior as before the wipe.
6. Startup no longer rewrites committed files: thesis/spec files are written only when absent; `deploy_spec` skips the YAML write when content is byte-identical.

**Dependencies:** R1 (so restored agents can actually decide).

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r2-single-seed-path` | forge.py delegation, startup spec reconciliation, file-write hygiene |
| `r2-benchmarks` | benchmark seeding + fleet inclusion + evaluator/HoD exclusion + btc_hold sizing |

**Tests**
- `test_seeding.py::test_forge_boot_on_empty_db_matches_fresh_start` — both paths produce identical agent rows/configs/specs.
- `test_seeding.py::test_compiled_specs_reconciled_on_boot` — DB wiped of specs rows + spec files present → active specs restored for all compiled agents.
- `test_seeding.py::test_btc_hold_enters_and_holds` — passes the risk gate, enters once, does not re-enter.
- `test_meta_controller.py::test_benchmarks_never_evaluated_or_culled`.
- `test_rebuild_local_cache.py::test_rebuild_restores_compiled_roster`.

---

### Revision R3 — Deterministic counterfactual replay (no LLM)
**Goal:** Every `wait` decision gets a mechanically-computed counterfactual outcome from recorded candles, on schedule, with coverage visible — the calibration substrate the whole evolutionary loop depends on.

**Acceptance Criteria**
1. `run_counterfactual` is rewritten (new module `store/counterfactuals.py` is acceptable) to: select **all** wait decisions older than N hours lacking a counterfactual; for each, determine the hypothetical entry (asset + direction + thesis/spec-standard SL/TP — for compiled agents from their active spec; for LLM agents from per-agent defaults recorded at decision time or config); replay forward over `candles_5m` (ledger or DB) using the same first-cross semantics as `store/positions.find_first_cross`, including a max-hold timeout; write `counterfactual_result` (outcome, pnl_pct, exit_reason) and `counterfactual_was_better` = would-have-profited.
2. No LLM call anywhere in the counterfactual path.
3. The nightly job in `forge.py` calls it and is proven by an executed-job test (not just "scheduled").
4. Coverage (% of waits ≥24h old with filled counterfactuals) is computed and exposed at `/health` and on the decisions page (see R9). — closes M9 AC#7.
5. Waits with insufficient forward data remain unfilled (null), never guessed.

**Dependencies:** R1 (fleet produces real waits with confidence). Interacts with R4 (wait rows must carry asset/confidence context for compiled agents).

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r3-counterfactual-replay` | Replay engine + nightly job wiring + coverage metric |

**Tests**
- `test_counterfactuals.py::test_wait_replay_hits_tp` / `::test_wait_replay_hits_sl` / `::test_wait_replay_max_hold` — synthetic candle paths produce exact expected outcomes.
- `test_counterfactuals.py::test_backlog_processed_not_just_latest` — 50 unfilled waits → all filled in one run.
- `test_counterfactuals.py::test_insufficient_future_data_left_null`.
- `test_counterfactuals.py::test_no_llm_dependency` — module imports/behaves with no LLM available.

---

### Revision R4 — Honest compiled-agent telemetry and spec-parity execution
**Goal:** Compiled agents log real confidence/evidence on waits, enforce `max_hold_hours`, and hold at most one position per asset — matching what the backtester validated.

**Acceptance Criteria**
1. The compiled path tracks the best evaluation across assets even when below threshold, and wait logs carry that decision's actual `confidence` and `evidence_strength` (never hardcoded 0.0). The reason string reports the true best, not the threshold.
2. `reconcile_positions` (or a dedicated pass in the heartbeat cycle) closes any position whose age exceeds its spec's `max_hold_hours` with `exit_reason="max_hold"`. Spec metadata needed for this (max_hold at entry) is stored on the position row.
3. Compiled agents skip `enter` for an asset they already hold a position in.
4. (Scaled sizing) Either the interpreter returns a structured size multiplier that both live and backtest apply identically, or live stops scaling by confidence — no third state. Risk gate validates the *final* order.

**Dependencies:** R1/R2 (compiled roster live).

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r4-compiled-telemetry` | Wait confidence/evidence logging |
| `r4-exec-parity` | max_hold enforcement, per-asset guard, sizing unification |

**Tests**
- `test_decision_loop.py::test_compiled_wait_logs_best_confidence` — sub-threshold evaluation logs its actual confidence.
- `test_positions.py::test_max_hold_closes_position`.
- `test_decision_loop.py::test_compiled_no_duplicate_asset_position`.
- `test_backtest_parity.py::test_live_and_backtest_size_identically` — same spec + same feature rows → same size.

---

### Revision R5 — Paper measurement matches reality (and the backtester)
**Goal:** Fees, funding, and stop-outs in paper trading are computed the way an exchange would compute them, and live-paper vs backtest use one shared cost model.

**Acceptance Criteria**
1. True notional (`margin × leverage`) is stored explicitly (new column or renamed field); fees = `true_notional × taker_fee` per side; funding accrues on true notional position size. Existing rows are annotated, not silently reinterpreted (M6 voiding ethos).
2. SL/TP reconciliation scans candle wicks over the interval since the last reconcile for **every** open position each heartbeat (not only when the current price sits outside bounds), using first-cross semantics with SL-before-TP tie-break within a candle.
3. `backtest/engine.py` accrues funding PnL on open positions from the ledger funding series (rate × true notional per funding interval, sign by direction). A funding-only spec (e.g. silver_basin) backtested over a synthetic period with known funding shows the analytically-expected PnL.
4. One cost model, one place: paper bridge, `execute_close`, and the backtest engine share fee/funding computation (a small `execution/costs.py`), so parity can't silently drift again.
5. Trade IDs are collision-proof (monotonic suffix or uuid fragment).

**Dependencies:** independent; do alongside R3/R4.

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r5-cost-model` | Shared fee/funding module + paper bridge + execute_close + engine adoption |
| `r5-wick-reconcile` | Reconciliation rewrite + tests |

**Tests**
- `test_costs.py::test_fees_on_leveraged_notional` — 5× leverage, $5k margin → fees on $25k.
- `test_costs.py::test_funding_sign_and_magnitude` — long pays positive funding; exact arithmetic.
- `test_positions.py::test_wick_through_sl_within_candle_closes` — price wicks through SL and recovers within one candle → position closed at SL.
- `test_backtest_engine.py::test_funding_pnl_accrues` — funding-only spec earns the computed carry.
- `test_backtest_parity.py::test_paper_and_backtest_costs_identical`.

---

### Revision R6 — Risk officer rebuilt to the proposal's contract, and actually consumed
**Goal:** A reduce-only mid-loop that reads real config, throttles gross exposure, enforces event blackouts, and whose verdicts the decision path obeys — with human and automatic disables that behave sanely.

**Acceptance Criteria**
1. All config reads come from the `desk` section with correct units and sane defaults derived from account scale (no more $1,000/$100/$500 magic numbers). Any *_pct value is a fraction used as a fraction.
2. `decision_loop` checks the entry gate before executing an `enter` (positions may still close while disabled) and logs `risk_blocked` decisions with the disable reason. This makes the human UI toggle real too.
3. Automatic disables are idempotent (no row spam), carry `disabled_by='risk_officer'`, and **auto re-enable** when the triggering condition clears; human disables (`disabled_by='human'`) are only cleared by a human.
4. Gross-exposure throttle: aggregate true notional > (configurable) 2× desk equity → entries disabled on the highest-exposure agents until under the line (proposal AC#5b).
5. Event blackout: no new entries desk-wide within 2h before calendar macro events (FOMC/CPI), reading `market/event_calendar.py`'s data (AC#5c).
6. A reduce-only validator: the officer's outputs structurally cannot increase size, loosen a stop, or add entries — enforced by construction and by test (AC#5e).
7. Daily-loss accounting uses exit timestamps.

**Dependencies:** R5 (true notional) for exposure math; R2 for stable roster.

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r6-risk-officer` | Config, throttle, blackout, reduce-only validator |
| `r6-gate-consumption` | decision_loop gate check + disable lifecycle |

**Tests** (the proposal's own M9 table, finally implemented)
- `test_risk_officer.py::test_gross_exposure_throttle`
- `test_risk_officer.py::test_event_blackout_blocks_entries`
- `test_risk_officer.py::test_risk_officer_cannot_add_risk`
- `test_risk_officer.py::test_auto_disable_reenables_when_clear`
- `test_risk_officer.py::test_human_disable_survives_officer_cycles`
- `test_decision_loop.py::test_entry_blocked_when_gate_closed`

---

### Revision R7 — Selection pressure done right: evaluator fixes, spawn discipline, Head of Desk as specified
**Goal:** The lifecycle rules terminate/suspend for the right reasons only; spawning is deliberate, graveyard-checked, and seed-fed; the Head of Desk becomes a briefing + chat synthesizer, not a random agent mill.

**Acceptance Criteria**
1. **Evaluator:** with no/insufficient null data, the not-beating-null rules return "insufficient_data" (no suspend/terminate). Suspended agents always reach the restore-or-terminate branch (the drawdown check no longer short-circuits it into permanent limbo). Zero-trade agents are exempt from repeated evaluations rows (evaluate only when trades since last eval ≥ interval), and the zero-trades-in-5-days review also fires for agents that have *never* traded. Statistical note recorded in the eval row: current p-value is a Sharpe-separation approximation; acceptable short-term, but label it as such in `metrics_json`.
2. **Spawning:** the archetype auto-spawner is **deleted**. All spawns route through one function that (a) calls `check_against_graveyard` and refuses on rejection, (b) consumes the `seeds` table when available, (c) names agents via `generate_agent_name` (adjective_animal), (d) writes a real thesis document. Population maintenance spawns at most one agent per cycle and reads `desk.target_agent_count`.
3. Orphan junk agents/theses (`agent_momentum_1`, `agent_mean_reversion_2` files) are removed from the working tree.
4. **Head of Desk (proposal AC#6):** a daily briefing job (desk P&L, per-agent deltas, regime note, pending human actions, coverage stats) written to `evaluations` (or a `briefings` table) and rendered on the overview; a `/chat` endpoint with query tools over trades/decisions/graveyard using the reflection-grade model. WebSocket streaming is optional in v1; a request/response chat with persisted `chat_history` satisfies this revision.
5. Harvest→spawn loop closes: `test_spawn_from_harvest` — a terminated agent's seeds can parameterize the next spawn.

**Dependencies:** R2 (benchmarks for the null), R1.

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r7-evaluator-fixes` | Null-absence semantics, suspension paths, eval cadence |
| `r7-spawn-discipline` | Single spawn path + graveyard wiring + seeds consumption + junk removal |
| `r7-head-of-desk` | Briefing job + chat + overview panel |

**Tests**
- `test_meta_controller.py::test_no_null_data_never_terminates`
- `test_meta_controller.py::test_suspended_agent_reaches_restore_or_terminate`
- `test_meta_controller.py::test_never_traded_agent_flagged_for_review`
- `test_spawner.py::test_spawn_rejected_by_graveyard_is_blocked`
- `test_spawner.py::test_spawn_from_harvest`
- `test_head_of_desk.py::test_daily_briefing_produces_text` (references ≥1 agent by name)
- `test_head_of_desk.py::test_chat_query_returns_trade_data`

---

### Revision R8 — Reflection that can actually improve a trader
**Goal:** The reflection pipeline gets real inputs, real gates, a real log, and can only deploy specs that beat what they replace on held-out data.

**Acceptance Criteria**
1. **Wiring:** the scheduler passes a reflection config containing `ledger_dir` (the repo's `ledger/`) and the real desk config (so `deploy_spec` validates). The reflection `llm_fn` is a raw-text contract (prompt in → text out) via a completion call — **not** `model_chain.decide`'s decision-JSON parser — and the model used is the configured reflection-grade model, recorded in the reflection row.
2. **Log:** `run_reflection` inserts a `reflections` row at trigger time (agent, `triggered_at`, inputs summary) and updates it with outcome/gate/spec-version — eligibility windows and AC#1's queryable log both become real.
3. **Real holdout gate:** the last-20-trade window is replayed through both the current spec and the revised spec (backtest engine over the ledger for the holdout period); revision blocked if materially worse (threshold configurable), per proposal §Anti-Overfitting 2.
4. **Real pattern persistence:** the gate tests the *feature condition* (evaluate the evidence term over ledger history) across ≥3 non-overlapping 7-day windows, not merely trade-history span.
5. **Cross-agent gate:** either implemented against `key_conditions_met`/trade-bank queries or explicitly removed with a logged "not_implemented" status — no silent always-pass.
6. **Walk-forward is mandatory** for deploys, not optional: a revised spec with no runnable walk-forward (insufficient data) is *rejected*, with that reason logged.
7. **Deploy guards:** never deploy a spec with zero evidence terms; never deploy to an agent not flagged `compiled`; deploys go through validation (non-None config) always.
8. Reflection prompt includes real backtest results (current spec's walk-forward summary), not `{}`.

**Dependencies:** R3 (counterfactual data), R2 (roster), R5 (engine funding for fair holdout replay of funding specs).

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r8-reflection-wiring` | llm contract, config, reflections rows |
| `r8-real-gates` | holdout replay, pattern persistence, mandatory walk-forward, deploy guards |

**Tests**
- `test_reflection.py::test_holdout_gate_rejects_worse_spec` — revised spec engineered to lose on holdout is blocked with gate name.
- `test_reflection.py::test_empty_evidence_spec_never_deploys`
- `test_reflection.py::test_control_arm_agent_never_gets_spec_deployed`
- `test_reflection.py::test_reflection_row_inserted_and_updated`
- `test_reflection.py::test_walk_forward_required_for_deploy`
- `test_reflection_schedule.py::test_trigger_fires_on_trade_count` (proposal M9 table)
- `test_reflection_schedule.py::test_eligibility_respects_last_reflection`

---

### Revision R9 — Command Deck: finish the safety contract
**Goal:** Executive actions are confirmed, audited, safe, and truthful; the missing pages exist; desk settings round-trip.

**Acceptance Criteria**
1. Confirmation token on every exec action (server-issued nonce or double-submit pattern); GET rejected; POST without token rejected (proposal M10 AC#6 + test).
2. Emergency stop: scopes to non-terminal agents (`WHERE status IN ('rookie','active','shadow','live','suspended')` — never `terminated`/`culled`), closes all open positions through the bridge path, writes one audit row per closed position + one for the stop.
3. Manual position close writes an audit row.
4. Promote-to-Shadow / Go-Live are removed or disabled ("M11 pending") until shadow/live execution exists — a status change must never silently stop an agent trading.
5. Reflect-Now routes through the (R8) reflection scheduler path in a background task; no `app.state.llm_fn` dependency.
6. `/decisions` page (decision log + counterfactual coverage from R3), `/graveyard` page, and (with R7) the briefing panel exist. `/chat` ships with R7.
7. Desk settings (wake cadence, reflection trigger, evaluation interval/thresholds, universe, risk caps) read/write the settings table and are consumed at runtime (scheduler intervals re-read or jobs rescheduled on save) — with the proposal's `test_settings_roundtrip_changes_behavior`.

**Dependencies:** R7/R8 for the actions they expose; independent otherwise.

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r9-action-safety` | Confirm tokens, emergency stop, audit completeness, placebo-button removal |
| `r9-pages` | /decisions, /graveyard, settings roundtrip |

**Tests**
- `test_web_actions.py::test_actions_require_post_and_confirm`
- `test_web_actions.py::test_emergency_stop_closes_positions_and_spares_graveyard`
- `test_web_actions.py::test_close_position_writes_audit`
- `test_web_settings.py::test_settings_roundtrip_changes_behavior`
- `test_web_desk.py::test_decisions_page_shows_counterfactual_coverage`

---

### Revision R10 — The integration harness (the test that was missing)
**Goal:** One command proves the assembled system works: boot the real `forge.py` composition against stub market data and a stub LLM chain, run several accelerated cycles, and assert the full loop end-to-end. This becomes the merge gate for every future revision.

**Acceptance Criteria**
1. A pytest-runnable integration test (marked `slow`/`integration`) that, with `data_source: stub` and a stub decision chain, boots the real startup path (seeding, spec reconciliation, schedulers) in-process with second-scale intervals.
2. Asserts, within the run: (a) every fleet agent logs a decision with non-null model; (b) at least one trade opens and closes via SL/TP reconciliation with fees/funding computed by the shared cost model; (c) benchmark agents trade; (d) the counterfactual job fills a wait; (e) meta-controller, risk-officer, and reflection-scheduler jobs each execute without exceptions; (f) ledger lines appear for decisions/trades/accounts; (g) `state/current.json` reflects the run.
3. A documented `make`/script entry point (`scripts/smoke_test.py` or pytest marker) referenced in `CLAUDE.md` as the required pre-merge gate.
4. CI-compatible runtime ≤ ~2 minutes.

**Dependencies:** R1–R3 minimum (it will fail, correctly, until they land — write it first as the executable definition of done).

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r10-smoke-harness` | Integration test + stub chain + docs |

**Tests:** the harness itself, plus `test_smoke.py::test_full_cycle_assertions` as above.

---

### Revision R11 — Repo hygiene
**Goal:** The working tree stops accumulating debris and side-effect dirt.

**Acceptance Criteria**
1. Root debris removed: `quick_test.py`, `test_import.py`, `test_event_import.py`, `test_unwrapping.py`, `test_syntax.bat`, `query_trades.py`, `query_trades2.py` (fold anything still useful into `tests/` or `scripts/`).
2. `.omo/` gitignored or removed; stale `.claude/worktrees/*` pruned (12 accumulated).
3. Orphan junk theses removed (with R7.3).
4. `git status` is clean after a normal `forge.py` boot + one heartbeat cycle, except ledger/state changes that `git_sync` itself commits (requires R2.6).
5. `CLAUDE.md` updated: the smoke-test gate (R10), single-seed-path rule, and "config keys live under `desk.` — module defaults must fail loudly on missing keys, not invent numbers" as a standing convention.

**Dependencies:** R2.6 for criterion 4. Otherwise independent — can run immediately.

**Suggested Worktrees:** `r11-hygiene` (single).

**Tests:** `test_hygiene.py::test_boot_leaves_tree_clean` (run boot in tmp clone; assert no unexpected dirty paths) — or fold into R10's harness.

---

### Revision R12 — Pre-run safety latches
**Goal:** Two small, immediate guards that stop the meta-controller and reflection scheduler from actively damaging an in-progress unattended run before their real fixes (R7, R8) land. This is not a substitute for R7/R8 — it's the minimum needed so a two-week run isn't sabotaged by code that was written for a desk state (seeded benchmarks, real specs) that didn't exist yet when it ran unattended.

**Why these two, specifically:**
1. **Meta-controller termination without a null.** `meta/evaluator.py`'s `get_lifecycle_decision` treats "not beating null" as `beats_null=False` whenever `null_metrics` is `None` or the benchmark sample is thin (see C3) — so *any* agent that reaches 100 closed trades during the run gets terminated regardless of whether it's actually any good. A benchmark like `random_walk` can hit 100 trades in days. Left unlatched, the meta-controller job (fires every 30 minutes per `forge.py`) will start culling the very agents the run exists to evaluate, before there's enough of a null distribution to justify it.
2. **Reflection deploying a hollow spec.** Per C8, once any agent crosses its trade-count threshold (default 20), the reflection scheduler will call a `model_chain.decide()`-shaped `llm_fn` against a YAML-spec prompt, get back a JSON decision dict, and `_dict_to_spec` will silently produce an all-defaults, zero-evidence spec — which `run_reflection`'s hollow gates wave through and `deploy_spec` will accept (validation is skipped because `config.get("desk_config")` reads a key that doesn't exist). That spec then overwrites `sage_turtle`'s real, hand-compiled spec — or gets attached to a control-arm agent that had no spec at all — silently ending that agent's run partway through the sample.

**Acceptance Criteria**
1. **Latch 1 (meta-controller):** before the run starts, either (a) the `meta_controller` scheduler job in `forge.py` is disabled for the duration (settings flag or commented job registration, clearly marked with a removal note pointing at R7), or (b) the minimal form of R7.1 lands: `get_lifecycle_decision`'s not-beating-null branches (`not_beating_null_50`, `not_beating_null_100`) return `{"decision": "active", ...}` (never suspend/terminate) whenever `null_metrics is None` or the benchmark's closed-trade count is below its own significance floor (e.g. 30).
2. **Latch 2 (reflection):** before the run starts, either (a) the settings table's `reflection_trigger` is set to `{"mode": "manual"}` for the duration (reflection never auto-fires; can still be triggered by hand for inspection), or (b) the minimal form of R8's deploy guard lands: `run_reflection` refuses to call `deploy_spec` when the parsed revised spec has zero evidence terms, logging a `rejection_reason` instead.
3. Whichever route is chosen for each latch, it is reversible via the existing settings mechanism (or a documented config flag) — no new schema required for the minimal version.
4. The run's operating notes record which latch mode was used for each (job-disabled vs. code-guard), so R6/R7/R8 — landing during or after the run — know exactly what they're replacing and can remove the temporary latch cleanly.
5. Both latches are verified against the live run itself, not just synthetically: after landing, `python forge.py` boots, the meta-controller and reflection-scheduler jobs are visibly scheduled (or visibly disabled) in the startup log, and the first-week behavior matches the chosen mode.

**Dependencies:** None structurally — config-only or a handful of lines. Practically: land it *last*, immediately before the run starts, after R1/R2/R4/R5/R3-capture/R10, so it's latching a desk that's actually able to trade.

**Note on the code-guard route:** choosing (b) over (a) for either latch is not throwaway work — it is a partial, early landing of R7.1 and R8's deploy guard respectively. There is no cost to doing it as code instead of a config toggle other than slightly more time up front, and it reduces R7/R8's later scope.

**Suggested Worktrees**
| Worktree | Scope |
|---|---|
| `r12-safety-latches` | Both latches, whichever route is chosen; startup log confirmation |

**Tests**
- If code-guard route: `test_meta_controller.py::test_no_null_data_never_terminates` (this is R7.1's test, landing early) / `test_reflection.py::test_empty_evidence_spec_never_deploys` (R8's test, landing early).
- If job-disable / manual-mode route: `test_forge_startup.py::test_meta_controller_job_disabled_via_config` / `test_reflection_schedule.py::test_manual_mode_blocks_all_triggers`.

---

## 8. Suggested sequencing

**Revised 2026-07-10** to answer directly: *what has to be true before starting an uninterrupted two-week paper-trading run?* The gate below is deliberately narrower than "finish M9 properly" — R6, R7, the rest of R8, R9, and R11 are real gaps but none of them make the run produce bad data or no data; they're quality-of-life and selection-pressure correctness that can land while the run is already in progress, or after.

```
PRE-RUN GATE (target: ~1 focused week, sequential where noted)
  1. R1                       — fleet LLM contract + pinned models         (hours)
  2. R2                       — single seed path, compiled roster restored,
                                 benchmarks seeded + btc_hold sizing fixed  (~1 day)
  3. R4                       — compiled wait telemetry, max_hold enforced,
                                 per-asset guard                            (~1 day)
  4. R5                       — true-notional fees/funding, wick-based
                                 stop reconciliation                        (1–2 days)
                                 [do not skip — a biased P&L sample from
                                 missed wick stop-outs cannot be repaired
                                 retroactively; it poisons every downstream
                                 decision the run's data feeds]
  5. R3 (capture half only)   — LLM/compiled wait decisions record the
                                 candidate asset + hypothetical SL/TP at
                                 decision time, so counterfactual replay
                                 can be backfilled later even if the full
                                 replay job (R3's remainder) lands mid-run   (~1 day)
  6. R10 (trimmed)            — smoke harness: boot the real composition,
                                 assert at least one trade opens and closes
                                 end-to-end before trusting it unattended    (1–2 days)
  7. R12                      — both pre-run safety latches (meta-controller
                                 null-absence guard or job-disable;
                                 reflection manual-mode or empty-spec
                                 deploy guard)                               (minutes–hours)
  8. Manual pre-flight: confirm Shadow/Go-Live buttons are not touched
     during the run (they silently stop an agent trading — see C9), and
     that Emergency Stop is avoided unless graveyard corruption is an
     accepted risk (or pull R9's emergency-stop fix into this gate too).

──────────────────────────  START THE TWO-WEEK RUN  ──────────────────────────

DURING THE RUN (code in parallel; do not deploy changes into the live desk
mid-sample unless fixing something the run itself breaks):
  R6   — risk officer rebuilt to spec (currently decorative; not harmful)
  R7   — evaluator fixes, spawn discipline, real Head of Desk / briefing
  R8   — reflection's real gates + holdout replay (land once ≥20 real
         trades and real counterfactual coverage exist to test against —
         this will be partway through the run)
  R11  — repo hygiene

AFTER THE RUN:
  R9   — Command Deck polish (confirm tokens, missing pages, safe
         emergency stop, settings roundtrip)
  Retire the R12 latches, replacing them with the completed R6/R7/R8 they
  were standing in for.
  First full unattended M9 "done when" attempt (reflection + evaluation
  running for real, on a desk whose latches have been removed).
```

The single most valuable thing after the pre-run gate closes is **uninterrupted runtime**. Every day of clean paper trading is the raw material for calibration, evaluation, reflection, and eventually the M11 promotion case. Resist rebuilding anything mid-run beyond what the run itself exposes as broken.

---

*Prepared by the desk's reviewing agent, 2026-07-09. Supersedes nothing; complements `docs/FORGE_PROPOSAL.md`'s Owner's Review of the same date, which this review found to be accurate in spirit (M9 was correctly identified as the highest-leverage work) but optimistic about what M8/M9/M10 code actually does when assembled.*
