# T6 Implementer Report (reconstructed by orchestrator)

The implementing session was interrupted before writing a report; this note reconstructs the claims from commit messages and verified test runs. Treat everything here as unverified claims to check against the diff.

- Commits: e99704d (three-stage reflection gates + atomic thesis/spec deploy, crit 3+4), 5c00f92 (atomic-deploy fix to catch deploy_as_challenger's silent validation failure; also carries the swept-in labeling/dossier work), d571910 (store/specs.py challenger pipeline + agents/decision_loop.py shadow-challenger block — files e99704d/5c00f92 depended on but which were missed in staging).
- Test evidence: full suite run at d571910 by the orchestrator: **660 passed, 1 skipped, 0 failed** (command: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -q --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py`).
- No TDD evidence available (original session lost). The new test files (test_labeling.py 561 lines, test_dossier.py 204 lines, test_reflection.py rewrite) are in the diff for direct inspection.
- Known placeholder: `store/specs.py::resolve_challenger` compares mean confidence, not mean labeled regret (regret rewire scheduled for a later task once `decision_labels` is populated by the labeling job).

## Fix round 1

Fixed all four T6 review findings. Commit `9b6d0e8` on `feat/r5-cost-model`: "m10: T6 review fixes — correlate close-decision labels to their trade, scope challenger baseline to trial window, bound forward-return staleness, cover shadow-challenger safety invariant".

### Finding 1 (critical) — `meta/labeling.py::_extract_close_info` wrong-trade correlation

Root cause: the query picked the agent's single most-recently-closed trade (`ORDER BY exit_timestamp DESC LIMIT 1`) with no correlation to the specific close decision being labeled.

Fix: `agents/decision_loop.py`'s `log_decision()` already writes `decision_details_json = {"position_id": ..., "fill": str(fill)}` for close actions, and `fill` (the return of `store/positions.py::execute_close`) always carries the exact `trade_id` that decision closed. `_extract_close_info` now parses that `trade_id` out of the `fill` field (via the existing `_safe_parse_dict` helper, same one used for enter/wait) and looks the trade up by `id = trade_id AND agent_id = ?`. When the trade_id can't be recovered (missing/malformed details, or the trade no longer exists), the decision is left unlabeled (returns `None`) rather than guessed.

Test evidence (TDD — written and confirmed failing against the buggy code before the fix landed, via a scratch scripted run of the old query logic against the new test fixtures):
- `tests/test_labeling.py::TestExtractCloseInfo` (3 tests): correct correlation via trade_id; missing decision_details_json → None; unknown trade_id → None.
- `tests/test_labeling.py::TestLabelCloseDecision::test_earlier_close_decision_labeled_from_correct_trade` — the required regression: an agent with two closed trades on different assets/directions (trade_a BTC-PERP/long closed first, trade_b ETH-PERP/short closed 2h later); only trade_a's asset has candle ledger data. Old code resolves to trade_b (most recent) → 0 candles found → `total_labeled == 0`. Verified this by running the old query logic standalone against the same fixtures: confirmed `{'total_processed': 1, 'total_labeled': 0, 'errors': 0}`. Fixed code correlates to trade_a correctly → `total_labeled == 3`.
- `tests/test_labeling.py::TestLabelCloseDecision::test_close_decision_without_correlatable_trade_left_unlabeled` — no matching trade → skipped, not guessed.
- There was previously no test exercising a close-action decision at all; now covered end-to-end via `run_labeling_job`.

Command: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_labeling.py -q` → 25 passed.

### Finding 2 — no test for the shadow-challenger safety invariant

Added `tests/test_challenger.py::test_challenger_logs_without_trading`. Deploys a real incumbent spec (long, confidence 0.6 ≥ threshold → full-size enter) and a real challenger spec (short, confidence 0.9, deliberately different direction/confidence so the two decisions are distinguishable) via `store/specs.py`'s real `deploy_spec`/`deploy_as_challenger`, then runs `agents/decision_loop.py::run_decision` end-to-end over a compiled agent.

Verification uses spies wrapping the REAL functions (not mocks that replace the code under test): `meta.risk_officer.RiskOfficer.entry_gate_status`, `agents.decision_loop.validate_order`, and the `bridge_factory` call site. Asserts:
- (a) only the incumbent's decision executes — exactly one trade created, direction "long" (never the challenger's "short").
- (b) exactly two decisions rows logged: a challenger shadow-log row with `challenger_spec_version: 2` / `incumbent_spec_version: 1` in `decision_details_json`, and a separate incumbent execution row with no `challenger_spec_version` key.
- (c) `gate_calls == [agent_id]`, `validate_calls == [1]`, `bridge_calls == [agent_id]` — exactly one call each, proving the challenger's decision (which itself would have qualified as an "enter") never reached the gate or bridge a second time.

Command: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_challenger.py -q` → 5 passed.

### Finding 3 — `store/specs.py::resolve_challenger` unbounded incumbent baseline window

Root cause: both the challenger and incumbent decision queries scanned the agent's entire decision history with no time bound, so the incumbent baseline included decisions logged under prior spec versions that predate the current challenger trial.

Fix: look up the challenger spec's `deployed_at` from the `specs` table (by `agent_id + status='challenger' + spec_version`) and scope the single decisions query (which already computes both challenger and incumbent sums in one pass) to `timestamp >= trial_start`. If `deployed_at` can't be found (shouldn't happen in practice), fails toward an empty window rather than silently falling back to the old unbounded query. The confidence metric itself is left as-is per the review note (documented interim placeholder ahead of a later mean-labeled-regret swap) — only the window scoping changed.

Test evidence (TDD — the pre-trial-exclusion test was verified against the old unbounded query via a standalone scratch run before the fix, confirming it flips the verdict from "promoted" to "rejected" without the window fix):
- `tests/test_challenger.py::TestResolveChallenger::test_promotion_path` — challenger avg > incumbent avg within trial window → promoted, specs/agents rows updated correctly.
- `test_rejection_path` — challenger avg ≤ incumbent avg → rejected, incumbent stays active.
- `test_no_challenger_returns_no_challenger_verdict`.
- `test_pre_trial_incumbent_decisions_do_not_influence_outcome` — rigged so including a pre-trial 0.99-confidence incumbent decision (logged before `deploy_as_challenger`) would flip "promoted" to "rejected"; confirms `incumbent_decisions == 1` (pre-trial row excluded) and `verdict == "promoted"`. Ran the same fixture through the old unbounded query standalone: confirmed it produces `{'verdict': 'rejected', ..., 'incumbent_decisions': 2}` — the bug this test guards against.

There was previously zero direct test coverage of `resolve_challenger`; now covered by the four tests above.

### Finding 4 — `meta/labeling.py::_fwd_return_at_cutoff` no staleness bound

Fix: added `CANDLE_INTERVAL_MS` / `STALENESS_THRESHOLD_MS` constants (2x the 5-minute candle cadence = 10 minutes), matching `scripts/build_training_dataset.py`'s documented `STALENESS_THRESHOLD` convention. `_fwd_return_at_cutoff` now returns `None` (horizon left unlabeled) when the nearest candle to the cutoff is farther than the threshold, instead of silently using whatever candle happened to be nearest regardless of distance.

Test evidence (TDD — written and confirmed failing against the old unbounded `min()`-only logic via a standalone scratch run before the fix):
- `tests/test_labeling.py::TestForwardReturnStaleness::test_candle_within_threshold_labels` / `test_candle_beyond_threshold_is_null` — direct unit coverage of the boundary.
- `test_gap_at_horizon_boundary_leaves_that_horizon_null` — the required gap regression: candles present 0–40min and 80min–24h10min (a 40-minute gap spanning the 1h/60min cutoff, distance 20min > 10min threshold), so the 1h horizon must be left null while 4h (240min, exact candle) and 24h (1440min, exact candle) label normally. Old code (verified via standalone scratch run of the pre-fix `min()`-only function against this exact fixture) produced `total_labeled == 3` with all three horizons present (including a 1h label built from a candle 20 minutes stale). Fixed code produces `total_labeled == 2`, horizons `{"4h", "24h"}` only.

Command: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_labeling.py -q` → 25 passed (staleness tests included in the same run as Finding 1's).

### Full suite

`C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -q --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py`

**673 passed, 1 skipped, 0 failed** (baseline 660 passed, 1 skipped + 13 new tests: 8 in `tests/test_labeling.py`, 5 in the new `tests/test_challenger.py`).

Files changed (explicit paths staged and committed, ledger/ and state/ left untouched):
- `meta/labeling.py`
- `store/specs.py`
- `tests/test_labeling.py`
- `tests/test_challenger.py` (new)
