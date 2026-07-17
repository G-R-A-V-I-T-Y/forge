# T6 Review Brief — M10 reflection gates, atomic deploy, challenger-deploy mechanics (+ swept-in labeling/dossier work)

## Scope of the diff under review

This diff (085aac3..d571910, three code commits: e99704d, 5c00f92, d571910) implements:

**Primary task (T6) — FORGE_PROPOSAL M10 acceptance criteria 3 + 4, plus the deploy-side mechanics of criterion 5:**

- **Criterion 3 (three-stage reflection):** `run_reflection` in `agents/reflection.py` becomes: Stage A `diagnose(dossier, llm_fn) -> list[Hypothesis]` — 1–5 hypotheses, each JSON with `claim`, `evidence_refs` (dossier item ids), `predicted_effect` (metric + direction + magnitude), `falsification_condition`. Stage B `propose(dossier, hypotheses, llm_fn) -> Proposal` — revised thesis markdown + revised spec YAML, each spec change annotated with the hypothesis id it serves. Stage C mechanical validation: parse → zero-evidence guard (kept) → complexity budget — at most `desk.max_evidence_terms` (default 4) evidence terms, and a spec adding terms beyond the incumbent's count must beat the incumbent's walk-forward deflated Sharpe → mandatory walk-forward — `run_walk_forward` on `config["ledger_dir"]` (a missing or too-short ledger is a hard, logged failure; the silent `except: skip` path removed) requiring deflated Sharpe > 0 and no parameter-sensitivity fragility flag. The always-pass stubs `check_holdout_split` and `check_cross_agent_validation` are DELETED. `check_min_trades` and `check_update_throttle` remain as pre-gates. The adversarial pass is demoted from gate to advisory: findings stored in `reflections.adversarial_critique` and appended to the revised thesis under "Known weaknesses"; LLM opinion never blocks a deploy.
- **Criterion 4 (atomic co-revision):** an accepted proposal writes `agents/theses/{agent}_v{N+1}.md`, inserts the `theses` row, bumps `agents.current_thesis_version`, and deploys the spec (recording `thesis_version = N+1`) — all or nothing; a failure at any step rolls back the rest. The reflection row fills `research_findings_json`, `proposed_changes`, `adversarial_critique`, `holdout_result`.
- **Criterion 5, deploy side only:** `store/specs.py` gains status `'challenger'` — `deploy_as_challenger`, `get_challenger_spec`, `resolve_challenger`. An accepted revision deploys as challenger, not active. `agents/decision_loop.py` gains a shadow-challenger block: each heartbeat the compiled body evaluates BOTH specs; the incumbent's decision executes; the challenger's decision is only logged (a `decisions` row with `challenger_spec_version` inside `decision_details_json`, never reaching the risk gate or bridge). SCOPE NOTE: the spec's criterion-5 resolution metric (mean labeled **regret**) and the resolution scheduler job are assigned to a later task (T8) in the plan, because regret needs the `decision_labels` pipeline running; this task's criterion-5 scope is the deploy/shadow-eval mechanics only. `resolve_challenger` in this diff uses mean confidence as an interim comparison. Evaluate how well the interim state is marked and structured for the T8 swap, and flag anything that would make that swap hard.

**Swept-in prior-session work (also in this diff, review against M10 criteria 1 + 2):**

- **Criterion 1 (forward labeling):** `meta/labeling.py` with `run_labeling_job(conn, ledger_dir)`: for every `decisions` row (enter, wait, close — all agents including benchmarks) whose timestamp sits at least the longest horizon behind the ledger head, compute from `ledger/candles_5m/`: forward return at 1h/4h/24h, max run-up and max drawdown per horizon, chosen-action outcome, best-available action among {enter-long, enter-short (thesis-standard SL/TP), wait}, and regret = best-available outcome − chosen outcome. Results in a `decision_labels` table (`decision_id, horizon, fwd_return_pct, max_runup_pct, max_drawdown_pct, chosen_outcome_pct, best_action, best_outcome_pct, regret_pct, labeled_at`) in the local cache, NOT a ledger stream. Job is idempotent, leaves labels null across ledger gaps (never interpolates), absorbs the M6 wait-only counterfactual filler (existing `counterfactual_*` columns keep being written), coverage % renders on `/decisions`.
- **Criterion 2 (evidence dossier):** `agents/dossier.py`: `build_dossier(conn, agent_id, ledger_dir) -> Dossier` (frozen dataclass with `to_prompt(max_chars)` that truncates by priority, never mid-record). Contents: full current thesis text + active spec YAML; last ≤50 closed trades with entry-fingerprint summary and postmortems; calibration curve; top-10 highest-regret labeled decisions with market context; win-rate/PF by regime; feature-conditioned statistics from the training dataset; the agent's hypothesis track record (registry lands in T8 — a graceful empty section is acceptable now); desk-memory digest (M11 — same).

## Spec tests expected in this diff

From the proposal's M10 test table: test_labeling.py (labels_computed_from_ledger, wait_regret_positive_when_entry_would_win, labeling_idempotent_and_gap_safe), test_dossier.py (dossier_includes_top_regret_decisions, dossier_respects_char_budget), test_reflection.py (diagnose_returns_falsifiable_hypotheses, walk_forward_gate_is_mandatory, complexity_budget_blocks_term_creep, adversarial_pass_is_advisory, thesis_and_spec_deploy_atomically), test_challenger-style coverage for logs-without-trading (may live in another test file — locate it in the diff). Regret-promotion/hypothesis-resolution tests belong to T8, not this diff.

## Known judgment calls to evaluate on the merits

- Derived fragility rule: 20% parameter perturbation flipping profitability counts as the fragility flag.
- Evidence-term count includes `secondary_evidence` terms toward the `max_evidence_terms` budget.

## Deferred items folded into this task (from T1 review)

- `tests/test_reflection.py::test_no_previous_spec_still_works` was supposed to get its docstring fixed and its lenient `deployed is True or rejection_reason is not None` OR-assertion tightened; `test_min_trade_gate`/`test_update_throttle` comments mentioning `desk_config` were to be cleaned. Verify this happened in the rewrite.

## Global constraints

- Config keys live under `desk.` in config.yaml; module defaults must FAIL LOUDLY on missing keys — never silently invent numbers. `config.get("desk_config")` is forbidden (key doesn't exist).
- llm_fn contract: `llm_fn(system_prompt, user_prompt) -> str` (reflection_client.complete shape).
- Reflection must never route through `llm/model_chain.py::decide()`.
- Labels are derived data: `decision_labels` is a local-cache table rebuilt by re-running the job — never a ledger stream, never interpolated across gaps.
- Challenger decisions must NEVER reach the risk gate or the paper bridge.
- The always-pass stubs `check_holdout_split`/`check_cross_agent_validation` must no longer exist in the codebase.
