# T8 Report — Challenger regret resolution + hypothesis registry wiring (M10 criteria 5+6)

Status: **DONE**

## Summary

All seven deliverables implemented. Full suite: 693 passed, 1 skipped, 0 failed
(baseline was 678 passed / 1 skipped; net +15 tests). Four logically-separate
commits on `feat/r5-cost-model`.

## Deliverables

### 1. Regret-based `resolve_challenger` (store/specs.py)

Rewrote the comparison to join `decisions` → `decision_labels` at a canonical
horizon and compare **mean labeled regret** instead of confidence parsed out of
`decision_details_json`. Both sides scoped to the challenger's trial window
(`timestamp >= deployed_at`, preserved from commit 9b6d0e8). Lower mean regret
wins.

- Challenger side: rows where `decision_details_json.challenger_spec_version ==
  challenger.spec_version`.
- Incumbent side: rows where `challenger_spec_version` key is **absent**
  (matches every real production shape — enter `{"order","fill"}`, wait
  `{"candidate":{...}}`/`NULL`, close `{"position_id","fill"}` — none of which
  ever carry that key).
- New `"not_resolvable"` verdict: when either side has zero labeled decisions
  in the window, neither spec row is touched (no promote/reject on zero
  evidence) — satisfies deliverable 1's explicit requirement.
- Return dict renamed `challenger_avg_confidence`/`incumbent_avg_confidence` →
  `challenger_mean_regret`/`incumbent_mean_regret`, plus
  `challenger_labeled_decisions`/`incumbent_labeled_decisions` and `horizon`.
  (Confirmed via grep: no other production code consumed the old field names —
  `resolve_challenger` was itself only called from the now-also-rewritten
  `check_challenger_resolution`.)

**Known-bug note for the record**: the brief states incumbent confidence
"lives in the `decisions.confidence` COLUMN" — I verified no such column
exists anywhere in `data/schema.sql` or any `ALTER TABLE` in `store/db.py`;
confidence is only ever written to the git-native ledger stream via
`log_decision`'s `append_ledger_record` call, never to a queryable DB column.
Doesn't change anything here (T8 moves off confidence entirely onto
`decision_labels.regret_pct`), flagging only because the brief's parenthetical
is inaccurate.

### 2. Resolution scheduler job (forge.py)

New hourly `IntervalTrigger` job `id="challenger_resolution"`: for every
agent with a `status='challenger'` spec row, calls
`check_challenger_resolution` (min-labeled-decisions/max-days trigger), and
when due, calls `resolve_hypotheses` for every reflection cycle whose
hypotheses are still `status='challenger'` for that agent, writing
`reflections.outcome = "challenger_{verdict}"`. Reads `config["desk"]`
directly (KeyError on missing section is caught by the job's own
try/except and logged as a job failure — "fails loudly" in the sense of
never silently defaulting the whole desk config, consistent with every
other job in forge.py which also wraps in try/except-log rather than
crashing the scheduler).

### 3. Hypothesis registration (agents/reflection.py)

`run_reflection` gained an optional `reflection_id: int | None = None` param.
`meta/reflection_scheduler.py::run_reflection_cycle` (the only production
caller that creates a `reflections` row) now passes `reflection_id=reflection_row_id`
through. Registration happens **immediately after Stage A parses hypotheses**
(before Stage 2 Propose is called) — not gated on the cycle ultimately
deploying — so a Propose-call transport failure or a later mechanical-gate
rejection never silently drops a hypothesis the LLM already stated. On a
successful atomic challenger deploy, those hypothesis ids are updated from
`'proposed'` to `'challenger'`.

Verified end-to-end (not just via the standalone registry functions) with a
new test in `tests/test_reflection.py` that runs the real three-stage
pipeline with a hypotheses JSON block in the Diagnose response and asserts
the resulting DB row.

### 4. Hypothesis resolution (agents/reflection.py)

`resolve_hypotheses` rewritten:
- `effect_observed` is now populated in every case it's computable:
  `incumbent_mean_regret - challenger_mean_regret` from `resolve_challenger`'s
  result (positive = challenger reduced regret). Previously this column was
  never written at all — the dead function only touched `status`/`resolved_at`.
- Status map: `promoted → validated`, `rejected → falsified`,
  `not_resolvable`/`no_challenger → inconclusive`.

**Judgment call — "predicted effect realized" rule**: I did NOT implement
per-hypothesis direction-matching (the brief's example rule). Stage A's
parser (`_parse_diagnose_hypotheses`) only extracts `claim`,
`evidence_refs`, `predicted_effect`, `falsification_condition` — never a
structured `direction` field — so there is nothing to match against
mechanically. Instead: a reflection cycle's hypotheses are the claims that
motivated *that cycle's* spec revision, and the challenger trial is the
out-of-sample test of the whole revision, so the trial verdict stands in for
the per-hypothesis check. This exactly matches the spec's explicit
allowance ("challenger rejected" alone is sufficient to falsify — no
separate falsification-condition text-matching needed) and collapses
cleanly onto the original (pre-T8, unwired) status map, now with
`effect_observed` actually populated and a `not_resolvable` branch added.

### 5. Dossier integration (agents/dossier.py)

`_get_hypothesis_history` now sources from
`agents.reflection.get_agent_hypothesis_history` (lazy-imported, matching the
existing `_safe_calibration` pattern, to avoid a module-load cycle since
`agents/reflection.py` imports `build_dossier` at top level) instead of the
unrelated `trades.hypothesis` free-text column it previously read. This is
the literal mechanism criterion 6 requires: an agent's own falsified ideas
now actually appear in its dossier. `Dossier.to_prompt`'s "HYPOTHESIS TRACK
RECORD" section rendering was updated to show `status`, `effect_observed`,
`claim`, and `falsification_condition` instead of the old
free-text-hypothesis/result fields.

### 6. Agent page rendering (web/app.py, web/templates/agent_detail.html)

New "Reflection Cycles" tab (`data-tab="reflect"` / `id="tab-reflect"`,
following the existing tab-bar convention exactly — no JS changes needed,
the tab-switcher is already generic). For each of the agent's 10 most recent
reflection rows: `research_findings_json` (dossier digest) and
`proposed_changes` (hypotheses + thesis/spec diff summaries) rendered as
formatted JSON, plus a table of that cycle's `hypotheses` rows
(claim/predicted_effect/status/effect_observed/resolved_at).

### 7. Tests

- `tests/test_challenger.py::TestResolveChallenger` — fixtures rewritten to
  production row shapes (helpers `_challenger_details`,
  `_incumbent_enter_details`, `_insert_labeled_decision`); added
  `test_not_resolvable_without_labels`.
- `tests/test_challenger.py::test_challenger_promotion_on_lower_regret`,
  `::test_challenger_rejection_resolves_hypotheses` — exact names from the
  brief.
- `tests/test_challenger.py::TestChallengerResolutionSchedulerWiring` —
  ast-based, matches `tests/test_labeling.py::TestForgeSchedulerAbsorption`'s
  convention.
- `tests/test_hypotheses.py::TestRegistryRoundtrip::test_registry_roundtrip`
  — exact name from the brief; covers proposed → challenger →
  validated/falsified/inconclusive with timestamps and effect_observed, plus
  a re-resolve-is-a-no-op assertion.
- `tests/test_hypotheses.py::TestCheckChallengerResolution` — labeled-vs-raw
  decision counting.
- `tests/test_reflection.py::TestEndToEndReflection::test_full_cycle_registers_and_transitions_hypotheses`
  — real pipeline, not just standalone functions.
- `tests/test_web_agent_detail.py` — two new tests for the Reflection Cycles
  tab (graceful-empty + full render).

## Judgment calls (summary)

1. **Horizon policy**: canonical single horizon = **4h** (not a per-decision
   average across available horizons). Avoids weighting bias toward older,
   more-labeled decisions and conflating short-term noise (1h) with slow
   signal (24h) into one number. `store.specs.RESOLUTION_HORIZON` constant;
   both `resolve_challenger` and `check_challenger_resolution`'s
   labeled-decision count use it consistently.
2. **"Predicted effect realized" rule**: cycle-level (challenger verdict),
   not per-hypothesis direction matching — see deliverable 4 above.
3. **Scheduler cadence**: hourly (`IntervalTrigger(hours=1)`) — matches the
   brief's suggestion; resolution is idempotent/cheap when nothing is due.
4. **"Window expired without signal"**: a forced resolution
   (`check_challenger_resolution` triggers on `max_days`) that still yields
   `resolve_challenger`'s `"not_resolvable"` verdict is *not* specially
   force-terminated — the challenger spec row is left as-is (trial keeps
   shadow-evaluating). Only that reflection cycle's hypotheses are marked
   `inconclusive` (terminal). If real labels land on a later scheduler pass,
   the trial can still resolve normally, but the already-`inconclusive`
   hypotheses from the earlier forced pass are not retroactively
   re-resolved (resolve_hypotheses only touches `proposed`/`challenger`
   rows). Documented as an accepted simplification, not silently left
   ambiguous.
5. **`get_hypothesis_digest`**: left with no new production call site. None
   of the 7 deliverables call for a cross-agent digest view (deliverable 5 is
   per-agent via `get_agent_hypothesis_history`; deliverable 6 is per-cycle
   hypothesis rows, queried directly). Added test coverage only, per
   "wire/adapt" instruction, without inventing an unrequested UI panel.

## Files changed

- `store/specs.py` — `resolve_challenger` rewrite, `RESOLUTION_HORIZON` const.
- `agents/reflection.py` — `run_reflection` reflection_id param + Stage A
  registration + challenger transition; `check_challenger_resolution` and
  `resolve_hypotheses` rewrites.
- `meta/reflection_scheduler.py` — thread `reflection_id` through.
- `forge.py` — new `challenger_resolution` scheduler job.
- `agents/dossier.py` — `_get_hypothesis_history` sources the registry;
  `to_prompt` section 8 rendering updated.
- `web/app.py` — `agent_detail` route gains `_get_reflection_cycles`.
- `web/templates/agent_detail.html` — new "Reflection Cycles" tab.
- `tests/test_challenger.py`, `tests/test_hypotheses.py` (new),
  `tests/test_reflection.py`, `tests/test_web_agent_detail.py`.

## TDD evidence

RED (pre-implementation): ran the rewritten `tests/test_challenger.py` +
new `tests/test_hypotheses.py` against the untouched confidence-based
`resolve_challenger` / dead registry functions — 12 failed, 5 passed
(the 5 passers were tests not exercising the changed logic, e.g.
`test_no_challenger_returns_no_challenger_verdict` and the shadow-logging
safety-invariant test).

GREEN: after `store/specs.py` rewrite, 2 failed / 10 passed (remaining
failures were the not-yet-implemented scheduler and hypothesis-effect
pieces). After `agents/reflection.py` rewrite, only the two ast-based
scheduler-wiring tests failed (forge.py not yet touched). After the
`forge.py` job was added, full `tests/test_challenger.py` +
`tests/test_hypotheses.py` + `tests/test_reflection.py` +
`tests/test_reflection_schedule.py` + `tests/test_m9_modules.py`: 100
passed.

## Self-review

- **Completeness**: all 7 deliverables implemented and covered by tests.
- **No overbuilding**: `get_hypothesis_digest` deliberately left uncalled in
  production (see judgment call 5); no new config keys added to
  `config.yaml` (the 20/7 defaults are spec-documented and read via
  `desk_config.get(key, default)`, matching the config convention: the
  top-level `desk` section itself is what must fail loudly, not each
  individual key).
- **Tests assert real behavior on production-shaped fixtures**: rewrote
  `TestResolveChallenger` fixtures away from the `{"confidence": ...}` shape
  production never writes, onto real `decision_details_json` shapes lifted
  directly from `agents/decision_loop.py`'s enter/wait/close/shadow-challenger
  log calls, plus `decision_labels` rows.
- **Pristine test output**: full suite run clean at 693 passed / 1 skipped /
  0 failed, no new warnings beyond pre-existing `pytest_freezegun` /
  starlette deprecation noise already present in the baseline.
- **Live-repo hygiene**: every commit staged explicit file paths only; never
  touched `ledger/` or `state/current.json` (confirmed via `git status`
  before and after — only the live heartbeat's own modifications to those
  paths appear, untouched by me).

## Concerns

None blocking. Two things worth a human's attention on the next pass
through this code (not gaps in scope, just forward-looking notes):

- The "stale inconclusive" edge case in judgment call 4 (forced-resolution
  hypotheses don't get a second chance if labels arrive later) is a real,
  if narrow, product behavior — worth confirming this is the desired
  semantics once real trial data starts accumulating on the live desk.
- `get_hypothesis_digest` remains genuinely dead in production (tested, not
  called). If a desk-wide hypothesis dashboard is wanted later (M11's
  desk-memory work looks like the natural home), it's ready to use as-is.

## Fix round 1

A task review of T8 flagged three findings. All three fixed; full suite
0 failed. This round's commit(s) touch only the files listed per finding
below — no `ledger/`/`state/` files staged (heartbeat-owned).

### Finding 1 (IMPORTANT) — web-triggered hypotheses never resolvable

**Root cause**: `web/app.py`'s `/api/exec/trigger-reflection/{agent_id}` and
`/api/exec/trigger-all-reflections` called `agents.reflection.run_reflection`
directly, never inserting a `reflections` row, so any hypotheses registered
during a web-triggered cycle got `reflection_id = NULL` — permanently
invisible to `forge.py`'s hourly resolution job, which filters
`AND reflection_id IS NOT NULL`.

**Fix**: both endpoints now call `meta.reflection_scheduler.run_reflection_cycle`
— the exact function `forge.py`'s scheduled job already used — instead of
calling `agents.reflection.run_reflection` directly. This reuses the single
existing INSERT site (no duplicate reflections-row creation code was added).
Updated the stale "ONLY production writer" comment in
`meta/reflection_scheduler.py::run_reflection_cycle` to name all three
callers (scheduler + both web endpoints) that now route through it.

Files: `web/app.py`, `meta/reflection_scheduler.py`.

Tests: `tests/test_web_actions.py::TestTriggerReflection::test_web_triggered_challenger_deploy_yields_resolvable_hypotheses`
(new) — drives the real `/api/exec/trigger-reflection/{agent_id}` endpoint,
stubbing only the LLM pipeline internals, registers+transitions a hypothesis
to `'challenger'`, then re-runs forge.py's exact resolution-job SQL shape
(`WHERE agent_id = ? AND status = 'challenger' AND reflection_id IS NOT NULL`)
and asserts it's found. Also updated `test_success`,
`test_reflect_endpoint_has_llm_fn`, `TestTriggerAllReflections::test_trigger_all_reflections_respects_eligibility`,
and `TestAuditLog::test_multiple_actions_all_logged` (all previously patched
`web.app.run_reflection`, which no longer exists as a call site — retargeted
to patch `agents.reflection.run_reflection`, the function
`run_reflection_cycle` actually invokes, and to return a real
`ReflectionResult` instead of `None`/an unconstrained `MagicMock`, since
`run_reflection_cycle` now post-processes the return value's fields).

Command: `pytest tests/test_web_actions.py -q` → **45 passed**.

### Finding 2 (IMPORTANT) — force-expired trials desync from hypotheses

**Root cause**: on `max_days` expiry with `resolve_challenger` returning
`"not_resolvable"` (zero labeled evidence), `forge.py`'s job flipped the
cycle's hypotheses to terminal `'inconclusive'` but left the challenger spec
row at `status='challenger'` — the trial kept running. If labels later
arrived and the trial resolved promoted/rejected, nothing could correct the
already-terminal hypotheses (`resolve_hypotheses` only touches
`'proposed'`/`'challenger'` rows). This was T8's own documented judgment
call 4, now treated as the bug it is.

A second, narrower desync also existed: `check_challenger_resolution`
treated `challenger_labeled >= min_decisions` as sufficient to call the
cycle "resolved" even when the *incumbent* side had zero labeled decisions
and `max_days` hadn't elapsed — i.e. not actually a window expiry, just the
count threshold firing lopsidedly. Fixed as part of the same change (see
invariant (a) below).

**Fix — status-vocabulary choice**: extended `store.specs.resolve_challenger`
with a keyword-only `force_close: bool = False` param. When `force_close=True`
and the zero-evidence branch would otherwise return `"not_resolvable"`, it
instead:
- sets the challenger spec row to **`status='inactive'`** — the same
  terminal status an ordinary rejection already uses (there is no separate
  `'rejected'` literal in the existing `specs.status` vocabulary; `'inactive'`
  is what both "challenger lost" and "incumbent demoted" already use, so this
  is the vocabulary-consistent choice, not a new status value), and
- records `rejection_reason = "challenger trial expired without sufficient
  labeled evidence (max_days elapsed)"` (the existing, previously-unused-by-
  `resolve_challenger` `specs.rejection_reason` column),
- returns verdict `"expired_no_signal"` (new, distinct from `"not_resolvable"`
  so callers/logs can tell an expired-empty trial apart from a still-live one).

`resolve_hypotheses`'s status map already defaults unrecognized verdicts to
`'inconclusive'` (`status_map.get(verdict, "inconclusive")`), so
`"expired_no_signal"` naturally resolves hypotheses to `'inconclusive'` with
`effect_observed=None` (no regret numbers exist) without any status-map
edit. `forge.py`'s job writes `reflections.outcome = f"challenger_{verdict}"`
generically, so the reflections row records `"challenger_expired_no_signal"`
— the "outcome recorded as expired-without-signal" requirement — with no
`forge.py` change needed.

`agents.reflection.check_challenger_resolution` now computes
`window_expired = days_elapsed >= max_days` and passes
`force_close=window_expired` to `resolve_challenger`. If `resolve_challenger`
still returns `"not_resolvable"` (only possible when `window_expired` was
False — the count-threshold-fired-without-incumbent-evidence case), the
function reports `resolved=False` instead of `resolved=True`, so the caller
(forge.py's job) skips it entirely — trial and hypotheses both untouched.

Files: `store/specs.py`, `agents/reflection.py`.

Tests (all three pinned behaviors):
- (a) pre-expiry not_resolvable → nothing changes:
  `tests/test_hypotheses.py::TestWindowExpiryDesync::test_pre_expiry_not_resolvable_leaves_everything_untouched`
  — challenger side clears `min_decisions`, incumbent has zero labels,
  `days_elapsed ~ 0 < max_days=7` → `resolved=False`, spec rows and
  hypothesis row (status/effect_observed/resolved_at) all unchanged.
- (b) post-expiry not_resolvable → trial closed + hypotheses inconclusive:
  `tests/test_hypotheses.py::TestWindowExpiryDesync::test_post_expiry_not_resolvable_closes_trial_and_hypotheses`
  — `deployed_at` backdated 10 days past `max_days=7`, zero labels either
  side → `resolved=True`, `verdict="expired_no_signal"`, challenger spec
  row → `'inactive'` with `rejection_reason` set, incumbent untouched; then
  (mirroring forge.py's job) `resolve_hypotheses` → hypothesis `'inconclusive'`
  with `effect_observed=None`, `resolved_at` set.
- (c) existing promoted/rejected paths unchanged:
  `tests/test_challenger.py::TestResolveChallenger::test_force_close_does_not_alter_promotion_verdict`
  and `::test_force_close_does_not_alter_rejection_verdict` — call
  `resolve_challenger(conn, agent_id, force_close=True)` with real evidence
  on both sides; verdict/spec-status transitions identical to the existing
  (force_close-less) promotion/rejection tests, `verdict != "expired_no_signal"`.
  Plus `::test_force_close_with_zero_evidence_closes_trial_terminally` — direct
  unit coverage of the new branch at the `resolve_challenger` level.

Command: `pytest tests/test_hypotheses.py tests/test_challenger.py -q` →
**23 passed** (10 + 13).

### Finding 3 (MINOR) — trigger-count vs evidence-count mismatch

**Fix**: added `AND dl.regret_pct IS NOT NULL` to
`check_challenger_resolution`'s labeled-decision count query in
`agents/reflection.py`, matching `resolve_challenger`'s own
`if regret is None: continue` skip in its mean-regret loop. Before this fix
a `decision_labels` row present at the canonical horizon but with
`regret_pct IS NULL` counted toward `challenger_min_decisions` in the
trigger but was silently skipped by `resolve_challenger`'s averaging —
letting the trigger claim enough evidence and then immediately land on
`not_resolvable`.

File: `agents/reflection.py`.

Test: `tests/test_hypotheses.py::TestCheckChallengerResolution::test_null_regret_pct_labeled_row_does_not_count`
(new) — inserts a decision_labels row at the canonical horizon with
`regret_pct = NULL`; asserts the trigger's count excludes it (`"0/1"`, not
`"1/1"`). Verified this test fails against the pre-fix query (temporarily
reverted the `AND dl.regret_pct IS NOT NULL` clause, confirmed
`AssertionError: assert '0/1' in 'trial in progress: 1/1 labeled
decisions, ...'`, then restored the fix and reran green) before finalizing.

### Full suite

`C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -q
--ignore=tests/test_forge_agent_timeout.py
--ignore=tests/test_forge_heartbeat_schedule.py`
→ **700 passed, 1 skipped, 0 failed** (baseline 693 passed / 1 skipped;
net +7 tests: 1 Finding-1 + 4 Finding-2 + 1 Finding-3 in the new/updated
test files, plus the 4 pre-existing web-actions tests updated in place
rather than added).

### Files changed (fix round 1)

- `store/specs.py` — `resolve_challenger` gains `force_close` param and the
  `"expired_no_signal"` terminal-close branch.
- `agents/reflection.py` — `check_challenger_resolution` computes
  `window_expired` and passes `force_close`; NULL-regret filter added to its
  labeled-decision count query.
- `web/app.py` — both trigger endpoints call `run_reflection_cycle` instead
  of `run_reflection` directly.
- `meta/reflection_scheduler.py` — comment update only (no behavior change).
- `tests/test_web_actions.py`, `tests/test_hypotheses.py`,
  `tests/test_challenger.py` — new/updated tests per finding above.

## Fix round 2

The from-scratch re-review verified all three round-1 fixes and flagged one
new Critical (A) and one new Important (B). Both fixed; full suite 0 failed.

### Fix A (CRITICAL) — challenger shadow decisions were never labelable in production

**Root cause**: `agents/decision_loop.py`'s shadow-challenger block logged
`decision_details_json` with only the six `challenger_*` keys.
`meta/labeling.py::_extract_decision_info` dispatches enter-action rows to
`_extract_enter_info` (requires a top-level `"order"` key) and wait-action
rows to `_extract_wait_info` (requires `"candidate"`) — neither key existed
in the shadow shape, so extraction returned `None`, `_label_one` wrote zero
`decision_labels` rows, and `resolve_challenger`'s zero-evidence guard fired
on every live trial: `not_resolvable` → (post round-1) `expired_no_signal`,
never `promoted`. Round-1 tests missed this because fixtures inserted
`decision_labels` rows directly, bypassing the production extraction path.

**Design choice — enrich the shadow log shape** (option 2), not teach
labeling a new shape (option 1). Rationale:
- The labeler needs `entry_price`/SL/TP **captured at decision time**; the
  old shadow shape never carried a price, so labeling could not reconstruct
  it later from the row alone — option 1 would have needed a ledger price
  lookup bolted into the extractor, a new code path with its own staleness
  semantics.
- Option 2 reuses the extractors' existing production contracts verbatim:
  the enter shadow now carries `"order"` as a `str()` repr (mirroring the
  incumbent enter branch's `str(response)`), the wait shadow carries
  `"candidate"` as a plain dict (mirroring the compiled-wait branch's
  candidate block, including `confidence`/`max_hold_hours`). Zero changes to
  `meta/labeling.py`.
- `challenger_spec_version` stays top-level — `resolve_challenger`,
  `check_challenger_resolution`, and the shadow-safety test all key on it,
  and its presence still cleanly separates challenger rows from incumbent
  rows.
- **Backward tolerance**: old-shape shadow rows (already logged on the live
  desk) hit the extractors' existing `return None` path — skipped, zero
  labels, zero errors, no crash. That is labeling's pre-existing behavior
  for unextractable rows; no code change needed, pinned by a test.

The SL/TP context uses the challenger spec's own
`stop_loss_pct`/`take_profit_pct` around the heartbeat price for the shadow
asset — the same convention the incumbent enter/wait branches use. When the
heartbeat has no positive price for the asset, the enrichment is skipped
(the row degrades to the old shape and is skipped by labeling, matching the
incumbent wait branch's own `if price and price > 0` guard).

File: `agents/decision_loop.py` (shadow-challenger block only).

**Mandatory closing-the-hole tests** (all in `tests/test_challenger.py`, no
direct `decision_labels` inserts anywhere):
- `test_enter_shadow_labelable_by_production_pipeline` — deploys incumbent
  v1 + challenger v2, runs the REAL `run_decision` (real risk gate, real
  PaperBridge) so it logs a genuine enter-action shadow row, backdates the
  cycle 25h and writes a synthetic SOL-PERP 5m candle ledger via
  `store.ledger.append_ledger_record`, runs the REAL `run_labeling_job`,
  then asserts (1) a `decision_labels` row with non-null `regret_pct` exists
  for the shadow decision at the canonical 4h horizon, and (2)
  `resolve_challenger` reports `challenger_labeled_decisions > 0` with a
  promoted/rejected verdict — trial resolvable end-to-end on
  production-pipeline output alone.
- `test_wait_shadow_labelable_by_production_pipeline` — same harness with
  the challenger's `scale_threshold` raised to 0.95 so its 0.9-confidence
  evaluation logs a wait-action shadow; same two assertions.
- `test_legacy_shadow_shape_skipped_not_crashed` — inserts the exact
  pre-fix shadow shape, runs the real `run_labeling_job` over a candle
  ledger; asserts zero errors and zero labels (skip, don't crash).

TDD evidence: both e2e tests written first and confirmed RED against the
unmodified decision loop (`assert label is not None` failed with `None` —
the precise production symptom); GREEN after the enrichment; the legacy
tolerance test passed both before and after (pinning that no behavior
regressed for old rows).

### Fix B (Important) — reflections.outcome only written when hypotheses existed

**Root cause**: in `forge.py`'s hourly job, the
`UPDATE reflections SET outcome = ...` sat inside `for hr in hyp_rows:` — a
resolved trial whose deploying cycle registered zero hypotheses (legacy
pipeline path, or Stage A parsed none) never got `reflections.outcome`
written, showing PENDING forever on the agent page. Spec requires two
unconditional actions: outcome lands in reflections AND hypotheses resolve.

**Fix**: extracted the job's per-agent body into
`agents/reflection.py::apply_challenger_resolution(conn, agent_id,
desk_config)` — forge.py's job now just loops challenger agents and calls
it (extraction was necessary for a behavioral test: forge.py imports
apscheduler at module load, which is not installed in the test env, so the
nested job closure was untestable). Inside the extracted function the
outcome write is unconditional on resolution: hypotheses (when any) are
resolved per cycle as before, and when NO hypothesis rows link a cycle to
the trial, the outcome lands on the most recent `outcome='deployed'`
reflections row for the agent — the cycle that deployed this challenger
(`run_reflection_cycle` writes `'deployed'` on every successful deploy,
scheduled and web-triggered alike after round-1's Finding 1 fix). If
neither exists (no hypotheses AND no deployed row — only reachable by
paths that bypass `run_reflection_cycle` entirely), a warning is logged;
there is genuinely no row to write to.

Files: `agents/reflection.py` (new `apply_challenger_resolution`),
`forge.py` (job body now a single call), `tests/test_challenger.py`
(`TestChallengerResolutionSchedulerWiring::test_challenger_resolution_job_invokes_check_and_resolve`
updated: the ast check now asserts the job calls
`apply_challenger_resolution(`, with the check/resolve/outcome behavior
covered behaviorally instead of by source-grep).

Tests (`tests/test_hypotheses.py::TestApplyChallengerResolution`, written
RED-first — ImportError before the function existed):
- `test_zero_hypotheses_cycle_still_records_outcome` — the Fix B core:
  deployed challenger + labeled evidence, a `'deployed'` reflections row,
  NO hypotheses → after `apply_challenger_resolution`, `reflections.outcome
  == 'challenger_rejected'`.
- `test_with_hypotheses_resolves_both` — parity with pre-extraction
  behavior: hypothesis → `'validated'` with `resolved_at` set AND outcome
  `'challenger_promoted'` on the owning cycle.
- `test_unresolved_trial_changes_nothing` — in-progress trial →
  `resolved=False`, outcome stays `'deployed'`.

### Full suite

`C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -q
--ignore=tests/test_forge_agent_timeout.py
--ignore=tests/test_forge_heartbeat_schedule.py`
→ **706 passed, 1 skipped, 0 failed** (baseline 700/1/0; net +6 tests:
3 Fix A + 3 Fix B).

### Files changed (fix round 2)

- `agents/decision_loop.py` — shadow-challenger block enriched with
  `"order"`/`"candidate"` labeling context.
- `agents/reflection.py` — new `apply_challenger_resolution`.
- `forge.py` — `_run_challenger_resolution_job` delegates to it; comment
  updated.
- `tests/test_challenger.py` — 3 new Fix A tests + updated ast wiring test.
- `tests/test_hypotheses.py` — new `TestApplyChallengerResolution` (3 tests).
