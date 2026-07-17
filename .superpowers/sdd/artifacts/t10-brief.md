# T10 Brief — Desk memory + graveyard extension into hypothesis space (M11 criteria 1+2)

## Spec text (docs/FORGE_PROPOSAL.md lines 1232–1233, read them directly for authoritative wording)

Criterion 1 — **Desk memory.** New `meta/desk_memory.py`: `get_desk_digest(conn, max_items=20) -> str` summarizing the desk-wide hypothesis registry — the strongest validated hypotheses and ALL falsified ones, each with agent, regime context, effect size, and sample size. Every M10 dossier embeds this digest, so agent B reflects with agent A's paid-for lessons in context. Rendered as an overview panel and served at `GET /api/desk-memory`.

Criterion 2 — **Graveyard extends into hypothesis space.** `meta/spawner.py::check_against_graveyard` additionally rejects any seed thesis or spec revision that re-encodes a *falsified* hypothesis — matched on (feature, direction, overlapping `regime_context`) — citing the falsifying registry row in the rejection reason. The existing thesis-token similarity check is retained for prose-level duplicates. Reflection Stage C calls the same check before walk-forward, so the desk never re-spends backtest budget on known-dead regions.

## Current state notes (verified through T8 close-out)

- `hypotheses` table live in data/schema.sql (~line 182): id, agent_id, reflection_id, claim, feature, direction, regime_context, predicted_effect, falsification_condition, status ∈ proposed|challenger|validated|falsified|inconclusive, effect_observed, created_at, resolved_at. Registration + resolution fully wired (agents/reflection.py: register_hypotheses/resolve_hypotheses; forge.py hourly challenger_resolution job).
- `agents/reflection.py::get_hypothesis_digest(conn, limit)` already exists (currently DEAD code, test-covered) — a cross-agent digest helper. Read it first: either build `meta/desk_memory.py::get_desk_digest` on top of it / move-and-adapt it, or supersede it (then delete the dead function rather than leaving two digests).
- `agents/dossier.py` already has a desk-memory placeholder section (gracefully empty; hook noted in T6/T7 reviews). Wire it: every `build_dossier` embeds the digest.
- `meta/spawner.py::check_against_graveyard` exists (M8/M9): thesis-token similarity vs graveyarded agents; called on spawn. Read its call sites before extending.
- Reflection Stage C gate order (agents/reflection.py run_reflection): parse → zero-evidence → complexity budget → walk-forward. Insert the graveyard-hypothesis check BEFORE walk-forward per the spec ("never re-spends backtest budget on known-dead regions"); a rejection must name the falsifying registry row (id + claim) in rejection_reason and be logged like other gate rejections (blocked_by_gate).
- "Overlapping regime_context": regime_context is a free-ish text/tag field — define overlap pragmatically (e.g. case-insensitive token/tag intersection, or exact match when both non-null; treat null as wildcard-overlap or no-overlap — pick ONE rule, document it, test it). Matching requires feature AND direction equality PLUS regime overlap, per spec.
- Spec revisions: "re-encodes a falsified hypothesis" for a spec = the revised spec adds/keeps an evidence term on the falsified (feature, direction) under overlapping regime context. Seed theses: the spawner-side check applies to spawn candidates (seed thesis text / its derived spec). Keep the implementation shared — one matcher used by both spawner and Stage C.
- Overview panel + API: follow existing web/app.py + web/templates/overview.html conventions (see how the briefing/desk panels are built). `GET /api/desk-memory` returns the digest (JSON with the structured items + the rendered string is fine — keep it simple).

## Required tests (per proposal test table)

- `tests/test_desk_memory.py::test_digest_includes_falsified_with_context()` — digest lists falsified hypotheses with agent, regime, effect size.
- `tests/test_desk_memory.py::test_dossier_embeds_desk_digest()` — an M10 dossier for agent B contains agent A's validated hypothesis.
- `tests/test_spawner.py::test_falsified_hypothesis_blocks_spawn()` — a seed thesis re-encoding a falsified (feature, direction, regime) is rejected citing the registry row.
- Plus: Stage C pre-walk-forward rejection test (a proposal re-encoding a falsified hypothesis is blocked with the citation, walk-forward never runs); regime-overlap rule edge cases (match + non-match); API endpoint test; retained prose-similarity check still works (existing tests keep passing).

## Environment rules (mandatory)

- `python` on PATH is a silent no-op stub. ALWAYS use `C:\ProgramData\Anaconda3\python.exe`.
- Full suite: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -q --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py` — must end 0 failed (baseline noted at dispatch).
- LIVE repo: heartbeat commits `ledger/` + `state/current.json` every 5 min. NEVER `git add -A` / `git add .`; explicit-path staging only; never touch ledger/ or state/.
- Config under `desk.`; fail loudly on missing keys; `config.get("desk_config")` forbidden.
- Commit message prefix: "m11: ".
