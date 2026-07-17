# T7 Brief — Labeling wiring + dossier integration: close the remaining M10 criterion 1+2 gaps

## Background

Most of M10 acceptance criteria 1 and 2 is already implemented, committed, and code-reviewed (commits 5c00f92 + 9b6d0e8): `meta/labeling.py::run_labeling_job` with the `decision_labels` table (correct close-trade correlation, gap-safe staleness bound, idempotent), nightly scheduling in forge.py at 02:30 UTC plus a nightly `scripts/build_training_dataset.py` refresh, `agents/dossier.py::build_dossier`/`to_prompt` consumed by `run_reflection` Stage A, labeling coverage via `meta/labeling.py::get_labeling_coverage` rendered on `/decisions`, and test suites tests/test_labeling.py (25 tests) + tests/test_dossier.py. Do NOT rebuild any of that.

Your task is the two verified remaining gaps against the spec text, plus a final criterion checklist pass.

## Gap 1 — Absorb the M6 wait-counterfactual filler into the labeling job

Spec text (M10 criterion 1): the labeling job "absorbs the M6 wait-only counterfactual filler (the existing `counterfactual_*` columns keep being written for compatibility)".

Current state: forge.py schedules TWO independent nightly jobs — `_run_counterfactual_job` (02:00 UTC, `store/counterfactuals.py::run_counterfactual_replay`, fills `counterfactual_*` columns on wait decisions) and `_run_labeling_job` (02:30 UTC); the comment at forge.py:374 says the counterfactual filler "keeps running independently". Also `meta/labeling.py` has comments (lines ~215, ~400) noting it *mirrors identical logic* in store/counterfactuals.py — duplicated candle-reading/replay logic between the two modules.

Required end state:
- ONE nightly job: `run_labeling_job` also produces the `counterfactual_*` columns for wait decisions (compatibility outputs), and the separate scheduled counterfactual job in forge.py is removed. The simplest correct absorption is for `run_labeling_job` to invoke the existing `run_counterfactual_replay` logic (or call into it) as part of its run, so the counterfactual columns keep exactly their current semantics — do not re-derive them with subtly different math. If you instead unify the duplicated candle-read/replay helpers so both outputs come from one code path, the counterfactual column values must remain identical to what store/counterfactuals.py produces today (prove it in a test comparing both paths on the same fixture).
- Do NOT delete `store/counterfactuals.py`'s public API — other code (web/app.py, calibration/pattern-persistence gates) reads the columns and `get_counterfactual_coverage`; only the standalone scheduler wiring goes away.
- Manual/other trigger paths for the counterfactual replay (if any exist — check web/app.py and scripts/) must keep working or be rewired to the absorbed path.
- Update the forge.py scheduling comments to match reality.
- Test: after `run_labeling_job` on a fixture with unfilled waits, both `decision_labels` rows AND `counterfactual_*` columns are populated; the standalone job id ("counterfactual") is no longer registered in forge's scheduler (check how existing forge scheduler tests assert job registration, if any).

## Gap 2 — One generalized coverage surface on /decisions, not two

The plan requires T7 to generalize the M9 counterfactual-coverage surface into the forward-labeling coverage surface — "reuse that surface, don't build a second one." Current state: web/templates/decisions.html renders two stacked panels ("Counterfactual Coverage" at lines 5–22 and "Forward Labeling Coverage" at lines 24–40), backed by two context vars in web/app.py (`coverage` and `labeling_coverage`).

Required end state: a single coverage section whose headline is the forward-labeling coverage (% of labelable decisions labeled — the M10 criterion-1 number), with the wait-counterfactual fill stats folded in as secondary stats within that one section (they remain useful and the columns keep being written per Gap 1). Adjust web/app.py context accordingly. Update tests/test_web_decisions.py to match.

## Checklist pass (verify, fix only if broken)

Walk M10 criteria 1 and 2 in docs/FORGE_PROPOSAL.md (lines 1178–1179) as a checklist against the current code and confirm each clause is real; fix anything you find missing. Known-acceptable deferrals: the dossier's "hypothesis track record" section may be gracefully empty until T8 lands the `hypotheses` table, and the "desk-memory digest" until M11 — but the dossier structure should already have those hooks/sections.

## Environment rules (mandatory)

- `python` on PATH is a silent no-op stub. ALWAYS use `C:\ProgramData\Anaconda3\python.exe`.
- Full suite: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -q --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py` — must end 0 failed (baseline: 673 passed, 1 skipped).
- LIVE repo: a heartbeat process commits `ledger/` and `state/current.json` every 5 min. NEVER `git add -A` / `git add .`; stage each changed file by explicit path. Never touch ledger/ or state/.
- Config keys live under `desk.` in config.yaml; fail loudly on missing keys; `config.get("desk_config")` is forbidden.
- Commit message prefix: "m10: ".
