# T9 Brief — Global trial accounting + bootstrap null (M11 criteria 3+4)

## Spec text (docs/FORGE_PROPOSAL.md lines 1234–1235, read them directly for authoritative wording)

Criterion 3 — **Global trial accounting.** New `backtest_trials` table (`id, spec_hash, agent_id, ran_at, data_window_start, data_window_end, deflated_sharpe, outcome`); every `run_walk_forward` invocation inserts one row. `backtest/walk_forward.py::_deflated_sharpe` takes `n_trials` = 1 + the count of desk-wide trials in the trailing 90 days whose data windows overlap the candidate's. Desk trial count and desk-level deflated Sharpe render on the overview.

Criterion 4 — **Bootstrap null.** `meta/evaluator.py::significance_test` replaces the `2/sqrt(N)` normal approximation with a resampling test: 1,000 bootstrap resamples (each of size = the agent's closed-trade count) of `benchmark_random_walk`'s per-trade returns build the null Sharpe distribution; the agent's empirical percentile is its p-value. The R12 insufficient-data latch (null < 30 trades → never cull on null comparison) is PRESERVED. Lifecycle rules consume the bootstrap p-value unchanged.

## Current state notes

- `backtest/walk_forward.py::run_walk_forward(spec, ledger_dir, taker_fee)` currently computes `_deflated_sharpe(..., n_trials=len(perturbable)+1, ...)` (line ~90) — a fixed local count. You are rewiring `n_trials` to the desk-wide overlapping-window count. Adding a DB `conn` parameter to `run_walk_forward` is expected (it must both insert the trial row and count prior overlapping trials). Update ALL call sites (grep for run_walk_forward — agents/reflection.py Stage C is the main production caller; run_reflection has `conn` available). One row per run_walk_forward invocation (the perturbation sweep inside does NOT insert extra rows).
- `data/schema.sql` uses CREATE TABLE IF NOT EXISTS convention — add `backtest_trials` there (plus any index you need on ran_at/window columns). Check how existing code ensures schema on the live DB (schema is applied idempotently) so the new table exists at runtime.
- `spec_hash`: a stable hash of the spec YAML/body — check store/specs.py for an existing spec-hash convention before inventing one.
- `outcome`: record the walk-forward verdict (e.g. passed/failed_deflated_sharpe/failed_fragility) — keep the vocabulary simple and documented.
- "Data window overlap": two trials overlap when their [data_window_start, data_window_end] intervals intersect. Trailing 90 days = ran_at within 90 days before the candidate's run.
- `meta/evaluator.py::significance_test` and the R12 latch: read the current implementation and its tests (tests/test_evaluator.py or tests/test_m9_modules.py — locate them) before changing anything. Lifecycle consumers (meta/controller.py) must keep working unchanged — same return contract (p-value semantics: LOW p = agent beats the null, or whatever the current convention is — PRESERVE the existing consumer-facing convention exactly; verify against how controller.py uses it).
- Bootstrap must be deterministic in tests — seed the RNG via a parameter or numpy Generator injection, not global seeding.
- Overview rendering: web/templates/overview.html + its route in web/app.py. Add desk trial count (trailing 90 days) and desk-level deflated Sharpe. "Desk-level deflated Sharpe" is not further specified: a defensible simple definition is the Sharpe of the desk's pooled closed-trade returns deflated by 1 + the trailing-90-day desk trial count. Decide, keep it simple, document in your report. (T12 later builds a fuller scoreboard; don't build that now — just these two overview stats.)

## Required tests (per proposal test table)

- `tests/test_walk_forward.py::test_global_trials_deflate_sharpe` — the same raw Sharpe yields a STRICTLY LOWER deflated Sharpe after 100 additional overlapping-window desk trials.
- `tests/test_evaluator.py::test_bootstrap_p_value_calibrated` — an agent sampled from the null itself is rejected at ≈ the nominal rate over many synthetic runs (keep runtime reasonable — a few hundred synthetic runs with a seeded RNG; tolerance band, not exact equality).
- `tests/test_evaluator.py::test_bootstrap_respects_insufficient_data_latch` — a null with <30 trades produces no cull decisions from null comparison.
- Plus: a backtest_trials insertion test (run_walk_forward inserts exactly one row with correct window bounds), and updates to any existing walk-forward/evaluator tests broken by the signature change.

## Environment rules (mandatory)

- `python` on PATH is a silent no-op stub. ALWAYS use `C:\ProgramData\Anaconda3\python.exe`.
- Full suite: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -q --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py` — must end 0 failed (baseline noted at dispatch time).
- LIVE repo: heartbeat commits `ledger/` + `state/current.json` every 5 min. NEVER `git add -A` / `git add .`; stage each changed file by explicit path; never touch ledger/ or state/.
- Config keys under `desk.` in config.yaml; fail loudly on missing keys; `config.get("desk_config")` forbidden.
- Commit message prefix: "m11: ".
