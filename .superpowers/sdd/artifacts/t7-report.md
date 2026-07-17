# T7 Report — Labeling wiring + dossier integration: M10 criterion 1+2 gap closure

Status: **DONE**

## Commit

`aa9cd2d` — "m10: absorb counterfactual filler into labeling job, unify /decisions coverage" (branch `feat/r5-cost-model`), 6 files changed, 281 insertions(+), 107 deletions(-).

Files touched (explicit paths only; `ledger/` and `state/current.json`, which the live heartbeat commits every 5 min, were never staged or touched):
- `forge.py`
- `meta/labeling.py`
- `web/app.py`
- `web/templates/decisions.html`
- `tests/test_labeling.py`
- `tests/test_web_decisions.py`

## Gap 1 — Absorb the M6 wait-counterfactual filler into the labeling job

**Implementation.** `meta/labeling.py::run_labeling_job` gained an optional third
parameter `config: dict | None = None`. After forward-labeling (or after the
early-return when there's no candle data), it now calls the existing,
byte-for-byte-unmodified `store.counterfactuals.run_counterfactual_replay(conn,
config or {}, ledger_dir)` and merges its summary into the return dict as
`result["counterfactual"]`. This is the "simplest correct absorption" path the
brief specified — no re-derivation of counterfactual math, no unification of the
duplicated candle-read helpers (left as-is; the brief explicitly permits keeping
them separate as long as the absorption itself calls the real function).

`forge.py`: removed the standalone `_run_counterfactual_job` function and its
`scheduler.add_job(..., id="counterfactual", hour=2, minute=0, ...)`
registration entirely. `_run_labeling_job` now calls
`run_labeling_job(conn, ledger_dir, config)`, threading desk config through to
the absorbed replay. Updated the scheduling comment block (previously said the
counterfactual job "keeps running independently" — now describes the absorption)
and the post-registration log line.

**Manual/other trigger paths.** Searched `web/app.py` and `scripts/` for any
other caller of `run_counterfactual_replay`: none exists. `web/app.py`'s
`/health` route only *reads* `get_counterfactual_coverage` (unaffected,
unchanged). No rewiring was needed beyond the scheduler.

**TDD evidence.**

RED (4 new tests, run before any implementation change):
```
$ C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_labeling.py -k "CounterfactualAbsorption or ForgeSchedulerAbsorption" -q
...
FAILED tests/test_labeling.py::TestCounterfactualAbsorption::test_labeling_job_also_fills_counterfactual_columns
FAILED tests/test_labeling.py::TestCounterfactualAbsorption::test_run_labeling_job_return_dict_includes_counterfactual_summary
FAILED tests/test_labeling.py::TestForgeSchedulerAbsorption::test_no_standalone_counterfactual_job_registered
FAILED tests/test_labeling.py::TestForgeSchedulerAbsorption::test_labeling_job_invocation_threads_config
4 failed, 25 deselected, 58 warnings in 1.03s
```

GREEN (after implementing the absorption in `meta/labeling.py` and `forge.py`):
```
$ C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_labeling.py -k "CounterfactualAbsorption or ForgeSchedulerAbsorption" -q
....                                                                     [100%]
4 passed, 25 deselected, 58 warnings in 0.49s
```

`test_labeling_job_also_fills_counterfactual_columns` builds a wait decision
with a candle wick through TP so the replay resolves deterministically
(`take_profit`, `profitable`) instead of depending on 48h of forward candle
coverage for a `max_hold_timeout` fallback, and asserts both `decision_labels`
rows (3, one per horizon) and the `counterfactual_result` /
`counterfactual_was_better` columns are populated after one `run_labeling_job`
call.

`TestForgeSchedulerAbsorption` reads `forge.py`'s source directly via `ast`
(the established convention in `tests/test_reflection_client.py` /
`tests/test_web_actions.py`, used because `forge.py` imports `apscheduler`)
and asserts `id="counterfactual"` is gone, `id="labeling"` remains, and
`_run_labeling_job`'s body literally calls
`run_labeling_job(conn, ledger_dir, config)`.

Full `tests/test_labeling.py` + `tests/test_counterfactuals.py`:
`59 passed` (no regressions in either module).

## Gap 2 — One generalized coverage surface on /decisions

**Implementation.** `web/app.py::decisions_page` now builds a single
`coverage` dict: `coverage = get_labeling_coverage(conn)` (the M10
forward-labeling headline numbers: `eligible_decisions`, `labeled`,
`coverage_pct`) with `coverage["counterfactual"] = get_counterfactual_coverage(conn)`
nested inside it (the M6 legacy stats: `eligible_waits`, `filled`,
`coverage_pct`, `total_waits`). The separate `labeling_coverage` context var
is gone; only `coverage` is passed to the template.

`web/templates/decisions.html` now renders one `<h3>Decision Coverage</h3>`
section: the primary `stat-row` shows forward-labeling coverage (the
criterion-1 number), followed by a `Wait-Counterfactual Fill (compatibility)`
sub-label and a second `stat-row` of secondary stats sourced from
`coverage.counterfactual`. This replaces the previous two independently
headed panels ("Counterfactual Coverage" / "Forward Labeling Coverage").

**TDD evidence.**

RED:
```
$ C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_web_decisions.py -q
...
FAILED tests/test_web_decisions.py::test_decisions_page_renders_with_coverage_tiles
FAILED tests/test_web_decisions.py::test_decisions_page_empty_desk
FAILED tests/test_web_decisions.py::test_decisions_page_single_coverage_context_var
3 failed, 1 passed, 12 warnings in 2.91s
```

GREEN (after updating `web/app.py` + `decisions.html`):
```
$ C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_web_decisions.py -q
....                                                                     [100%]
4 passed, 12 warnings in 2.07s
```

Updated existing tests to assert the single-section rendering
(`"Decision Coverage"` header, `"Wait-Counterfactual Fill"` sub-label present)
and added a new test (`test_decisions_page_single_coverage_context_var`) that
spies on the Jinja context passed to `decisions.html` and asserts
`"labeling_coverage"` is no longer a separate key and `coverage["counterfactual"]`
carries the folded-in stats.

## Checklist pass — M10 acceptance criteria 1 and 2 (docs/FORGE_PROPOSAL.md:1178-1179)

Walked both criteria clause by clause against current code:

**Criterion 1** — all clauses now hold:
- Nightly APScheduler job `run_labeling_job(conn, ledger_dir)` (now `..., config`) — yes, `forge.py` id="labeling", 02:30 UTC.
- Covers enter/wait/close, all agents including benchmarks — yes, the `decisions` table query has no agent-type filter.
- Computes fwd return/MFE/MAE/outcome/best-action/regret at 1h/4h/24h — yes, `_compute_horizon_label`.
- Writes to `decision_labels` (not a ledger stream) — yes.
- Idempotent — yes, `LEFT JOIN ... IS NULL` gate; tested (`test_idempotent`).
- Leaves labels null across gaps, never interpolates — yes, `STALENESS_THRESHOLD_MS` guard; tested (`TestForwardReturnStaleness`).
- **Absorbs the M6 wait-only counterfactual filler, `counterfactual_*` columns keep being written for compatibility** — was the Gap 1 defect; **fixed** this task.
- Coverage renders on `/decisions` — yes, was already true (`get_labeling_coverage` → `coverage` in the template); Gap 2 changed *how* it's presented (one surface) but the criterion's requirement was already met.

**Criterion 2** — all clauses hold, including the two permitted deferrals:
- `agents/dossier.py::build_dossier(conn, agent_id, ledger_dir) -> Dossier`, frozen dataclass with `to_prompt(max_chars)` — yes.
- Thesis text + active spec YAML, last ≤50 closed trades with fingerprint summary + postmortems, calibration curve, top-10 highest-regret labeled decisions with market context, win-rate/PF by regime, feature-conditioned stats from the training dataset — all present as dossier fields/builders.
- Hypothesis track record (criterion 6) — present as `hypothesis_history`, sourced from `trades.hypothesis`; gracefully empty until the `hypotheses` table lands in T8, per the brief's known-acceptable deferral. Structural hook already exists (`Dossier.hypothesis_history`, rendered as section 8 in `to_prompt`).
- Desk-memory digest (M11) — present as `Dossier.desk_digest`, currently always `""`, rendered as section 9 when non-empty. Hook already exists, per the brief's known-acceptable deferral.

No fixes were needed for criterion 2 — everything was already correctly built and the deferral hooks were already in place; verified by reading `agents/dossier.py` in full.

## Self-review

- **Completeness against the brief**: both gaps closed exactly as specified; no rebuild of already-shipped M10 work (`meta/labeling.py`'s core labeling logic, `agents/dossier.py`, the `decision_labels` schema were not touched beyond the additive Gap-1 change).
- **No overbuilding**: did not unify the duplicated candle-read helpers between `meta/labeling.py` and `store/counterfactuals.py` — the brief explicitly says this is optional and only required if I chose that path; I took the simpler "call into the existing function" path instead, which needed no such unification and carries lower regression risk. Did not touch the `/health` route's independent `get_counterfactual_coverage` read (out of scope — that's a different page, not one of the two `/decisions` panels).
- **Tests assert real behavior, not just presence**: the Gap 1 behavioral test drives a real `run_labeling_job` call through a synthetic ledger and asserts DB column values (not mocked); the scheduler test reads real `forge.py` source via `ast` following the codebase's established convention for that (`apscheduler` avoidance) rather than a weaker string-only check for the id assertion (the config-threading test uses `ast.get_source_segment` on the actual function body). The Gap 2 test spies on the real Jinja context via `TemplateResponse` patching rather than only checking rendered HTML text, catching both the context-shape and the rendering.
- **Pristine test output**: full suite run twice (once mid-implementation after Gap 2, once post-commit) — both `678 passed, 1 skipped`, 0 failed, 0 warnings beyond pre-existing deprecation noise (`pytest_freezegun`, Starlette `TemplateResponse` positional-arg deprecation) unrelated to this change.
- **Environment rules followed**: used `C:\ProgramData\Anaconda3\python.exe` throughout; ran the exact mandated full-suite command; staged only the six explicit file paths (`git add forge.py meta/labeling.py tests/test_labeling.py tests/test_web_decisions.py web/app.py web/templates/decisions.html`) — never `git add -A`/`.`, never touched `ledger/` or `state/current.json` even though the live heartbeat had modified them concurrently; commit message prefixed `"m10: "`.

## Concerns

None blocking. Two minor observations, neither requiring action:
1. `store.counterfactuals.run_counterfactual_replay`'s `config` parameter is (and was already, pre-T7) unused inside that function — `run_labeling_job` forwards desk `config` to it per the brief's guidance, but it currently has no effect until that function is changed to read from it. Left as-is; out of scope for T7.
2. Baseline full-suite count grew from 673 → 678 passed (5 new tests: 4 in `tests/test_labeling.py`, 1 in `tests/test_web_decisions.py`), which is expected and intentional, not a discrepancy.
