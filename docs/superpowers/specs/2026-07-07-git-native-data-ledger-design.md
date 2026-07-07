# Git-Native Data Ledger — Design Spec

**Date:** 2026-07-07
**Status:** Approved, pending implementation
**Scope:** Revises M7's `market_history` store (STRATEGIC_ASSESSMENT_2026-07-04.md §8) under the project's hard constraint that the whole system — code AND data — lives in one git repo, with no external database server. Does not cover the strategy-spec DSL, backtester, or liquidation-feed integration (those remain separate M7 work); does not cover agent roster changes (M8).

## 1. Problem

The original FORGE_PROPOSAL.md planned to commit `forge.db` to git directly (and even flagged "SQLite file grows too large for git" as a known risk). In practice `.gitignore` already excludes `*.db` and `data/historical_data/`, so today the git repo holds only code — all trade history, market data, and decisions live in local, gitignored files. A fresh `git clone` reproduces an empty system, not a working one.

The north star: **a burned laptop, replaced and `git pull`ed, resumes exactly where it left off — no skipped beat, no separate backup/restore step.**

## 2. Full data inventory

Assumptions: 10 agents, 20-asset universe, 5-minute heartbeat cadence.

| Data | Cadence | Why raw (not recomputed) | Rows/90d | Bytes/row | Size/90d |
|---|---|---|---|---|---|
| Candles 5m/1h/4h | 5m/1h/4h × 20 assets | Re-fetchable from exchange, but a local copy makes backtests self-contained | ~573K | ~20B | ~11MB |
| Funding | hourly × 20 | Same | 43,200 | ~12B | ~0.5MB |
| Open interest | 5 min × 20 | **Not retroactively available** — miss it live, it's gone | 518,400 | ~14B | ~7MB |
| Liquidations | event-driven | Same — proxy/live-only unless paying for Coinalyze | ~9,000 | ~40B | ~0.4MB |
| Event calendar | as scheduled | Small reference data | ~50 | ~150B | ~8KB |
| **Decisions — every cycle, every agent, wait/enter/close/risk_blocked/error** | 5 min × 10 agents | **The selection-bias fix.** "Wait" must be recorded as rigorously as "enter" | 259,200 | ~150B (structured evidence, not prose) | ~39MB |
| Trades (closed, fingerprints) | per trade | Existing design | ~390 | ~10KB | ~3.9MB |
| Accounts (equity curve) | per trade close (event-driven, matches current `execute_close` behavior) | Sharpe/Sortino input | ~390 | ~20B | negligible |
| Reflections / evaluations / thesis versions | ~weekly / on change | Evolution + selection audit trail | ~300 | ~1-3KB | ~0.5MB |
| **Total** | | | | | **~63MB / 90 days ≈ 250MB/year** |

Decisions dominate (60%+), precisely because "wait" gets full coverage. One deliberate exclusion from "everything": raw LLM prompt/response text (~50-65KB/call at ~10,800 tokens) is not archived — a `prompt_hash` plus the structured evidence fields is kept instead.

## 3. Architecture

```
ledger/                          ← git-tracked, append-only, immutable-once-closed
  decisions/2026-07.jsonl        ← hot: current month, appended every cycle
  decisions/2026-06.parquet      ← cold: closed month, compacted
  candles_5m/, candles_1h/, candles_4h/, funding/, oi/, liquidations/, trades/, accounts/, reflections/, evaluations/
  events.jsonl                   ← small, never partitioned
state/
  current.json                  ← open positions, live balances, agent status — tiny, overwritten + committed every cycle
data/forge.db                    ← gitignored, disposable local cache/query index — never the system of record
```

**Hot/cold split.** The currently-accumulating month is JSONL (pure byte-level append — the most git-friendly write pattern there is: git diffs it as "N lines added," never a rewritten file). A monthly job freezes the prior month and recompresses it to Parquet (columnar, 3-5x smaller). This is `data/historical_data/{YYYY-MM-DD}.jsonl` (Phase 1, already in the repo) generalized to every data type and actually wired to git instead of gitignored.

**Raw vs. derived.** Only raw inputs (candles, funding, OI, liquidations, decisions-as-made) are stored. Computed indicators (z-scores, correlations, regime tags) are recomputed from raw inputs by the backtester/analysis code at read time, never trusted as frozen historical fact — otherwise a later bug fix to a feature calculation silently corrupts every backtest that replays "history" through the old, wrong values. The one exception is the *decision* record itself: it must capture exactly what evidence/confidence the agent saw and acted on at the time, warts included, because the goal is calibrating the agent, not re-deriving a cleaner version of history.

**Commit and push every cycle, not batched.** New ledger data is a small append, not a file rewrite, so there is no efficiency reason to batch commits nightly — and batching directly conflicts with "no skipped beat," since it leaves a window of unsaved data. Thousands of small commits/year is normal git usage; the anti-pattern is repeatedly rewriting one large binary file (which this design does nowhere in the hot path). Local commits alone do not survive a burned laptop — **push**, not just commit, on every cycle. Best-effort, never blocks the heartbeat loop, same defensive pattern as `market/heartbeat.py`'s existing `append_historical()` (swallow exceptions, log, retry next cycle).

**Current state is also git-tracked**, not gitignored. `state/current.json` (open positions, balances, agent status — small, bounded by agent count) is overwritten and committed every cycle alongside the ledger. This is what closes the gap: `git pull` restores the exact last heartbeat's state, not "state as of last night's batch job." `data/forge.db` remains as a disposable, gitignored query cache for the web UI, rebuildable from the ledger + state at any time — never the thing you'd lose sleep over losing.

## 4. Growth and the escalation ladder

~250MB/year at this design (3yr ≈ 750MB, 5yr ≈ 1.3GB) — well inside GitHub's practical range; monthly partitioning keeps every individual file far below the 100MB/file hard limit. If verbose prose crept back into decision "reason" fields, the total roughly triples — the strongest argument for keeping evidence structured.

If it ever gets too big, in order of preference:
1. Tighten retention on the biggest stream first (decisions) — drop detailed evidence/candidates on wait-decisions older than N months, keep action+confidence+counterfactual only.
2. Resolution-decay old market data — 5m→1h beyond a rolling window (default 12 months).
3. Shard by year into separate repos (`forge-ledger-2026`, …) — still "everything in git," just not all in one clone.
4. Git LFS as a last resort for any single partition that risks the 100MB/file cap.

## 5. Explicit decisions made in this conversation

- **Same repo, not a separate ledger repo** — simplicity over history hygiene.
- **Structured evidence over prose** in decision records — this is what keeps the dominant volume driver small; it also requires the LLM prompt to actually ask for `confidence`/`evidence_strength` on `wait`, which it currently does not (see Task 3 of the plan).
- **Append-only means "closed-by-default, correctable via an explicit, audited, git-committed rewrite,"** not "sacred and never touched." Monthly compaction and resolution-decay are exactly this kind of deliberate, rare, auditable rewrite — as distinct from a hot-path process mutating a shared file every 5 minutes, which is the actual git anti-pattern this design avoids everywhere.
- **Existing corrupted trade/paper-trading data is wiped, not migrated.** It predates this schema and predates the M6 risk-gate fixes; there is nothing worth carrying forward. `scripts/fresh_start.py` already exists for this and is extended, not replaced.

## 6. Out of scope

Strategy-spec DSL, backtester, liquidation-feed integration (Coinalyze), agent roster changes (retiring gray_finch/amber_wolf, adding sage_turtle) — all separate M7/M8 work per STRATEGIC_ASSESSMENT_2026-07-04.md. `scripts/build_training_dataset.py` currently reads the old verbose `data/historical_data/*.jsonl` format, which this design retires; it will need a follow-up rewrite against the new ledger format, tracked separately (no parquet has ever been generated from it yet, so nothing is lost by deferring this).
