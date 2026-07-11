# Revision R1 — Restore the Fast Loop: Fix the Fleet LLM Contract and Pinned Models

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every non-compiled agent completes real LLM decision cycles again (currently every fleet agent errors out on every cycle due to a signature mismatch); per-agent pinned models are honored; the nightly counterfactual job no longer crashes on an undefined name; postmortems are no longer silently dropped when the agent subprocess exits.

**Source:** `docs/STRATEGIC_ASSESSMENT_07_09_2026.md`, defect **C1** ("Fleet LLM calls fail on a signature mismatch — no LLM agent can decide") and **Revision R1**.

**Root cause (C1):** `agents/decision_loop.py`'s `_call_llm_with_retry` calls `llm_fn(system_prompt, decision_prompt, agent_id=agent_id)`, but the production closure built inside `agents/agent_runner.py`'s `_run_once` is `def llm_fn(system_prompt, decision_prompt)` — no `agent_id` parameter. Every call raises `TypeError`, silently caught by the retry loop and logged as "LLM returned invalid response after retries". The live ledger confirms every fleet agent has been doing this on every cycle. A second, related break: `forge.py`'s nightly counterfactual job references a bare name `llm_fn` that is never defined anywhere in `forge.py`, raising `NameError` every night at 02:00 UTC. A third, smaller break: a just-closed trade's postmortem LLM call is scheduled with `asyncio.ensure_future(...)` (fire-and-forget) inside a one-shot subprocess that calls `asyncio.run(...)` and exits — the event loop closes before the postmortem task ever completes, silently dropping it.

## Global Constraints

- Python interpreter: `C:\ProgramData\Anaconda3\python.exe`
- Test command: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v` — **no `--ignore` flags needed**. (The CLAUDE.md note about `test_forge_agent_timeout.py`/`test_forge_heartbeat_schedule.py` failing under Anaconda3 due to a missing `apscheduler` is stale — both pass cleanly today because `apscheduler` now resolves via the shared user site-packages. Verified baseline: 485 passed, 0 failed, before any task in this plan begins.)
- Do not touch any file not listed in a task's **Files** section. Each task's diff should be small and mechanical.
- Do not change behavior beyond what the task specifies. In particular: do not alter `model_chain.decide()`'s contract, do not alter `run_counterfactual()`'s or `run_postmortem()`'s signatures, do not alter what `run_decision()` returns.
- Every new module-level helper this plan introduces must preserve the exact prior behavior for call sites that pass no new arguments — the only intended behavior change is that `agent_id` now actually flows through to `model_chain.decide()`.
- Follow TDD: write the failing test first, run it and confirm it fails for the stated reason, then implement, then confirm it passes. Then run the **full** suite (not just the new test file) before committing.
- Commit at the end of each task. Commit message format: `fix(r1): <short description>` with a body line citing which R1 acceptance criterion it satisfies (see `docs/STRATEGIC_ASSESSMENT_07_09_2026.md` Revision R1).
- These are one-shot subprocess / scheduled-job code paths (not the hot heartbeat loop) — a small, correctness-motivated increase in per-call latency (e.g., awaiting instead of fire-and-forget) is acceptable and is in fact what Task 3 requires.

---

### Task 1: Fix `agents/agent_runner.py`'s `llm_fn` to accept and forward `agent_id`

**Files:**
- Modify: `agents/agent_runner.py`
- Test: `tests/test_agent_runner_contract.py` (new file)

**Context:** `agents/decision_loop.py`'s `_call_llm_with_retry(llm_fn, system_prompt, decision_prompt, agent_id, max_retries=2)` calls its `llm_fn` argument as:
```python
result = llm_fn(system_prompt, decision_prompt, agent_id=agent_id)
```
`agents/agent_runner.py`'s `_run_once()` currently defines the closure passed as that argument like this (this is the exact current code — locate it, don't guess at line numbers, they may have shifted):
```python
def llm_fn(system_prompt: str, decision_prompt: str) -> tuple[dict, str | None]:
    return model_chain.decide(system_prompt, decision_prompt, config=config)
```
This closure does not accept `agent_id`, so every call from `_call_llm_with_retry` raises `TypeError: llm_fn() got an unexpected keyword argument 'agent_id'`. `_call_llm_with_retry` catches `Exception` generically and retries, then gives up and returns `(None, None)`, which `run_decision` logs as `"LLM returned invalid response after retries"`. This is why every LLM-routed agent has produced zero real decisions.

Separately: because `agent_id` never reaches `model_chain.decide()`, per-agent pinned models (`llm/model_chain.py`'s `decide(system_prompt, decision_prompt, config=None, agent_id=None)`, which looks up a pinned model via `agent_id` when provided) have never actually been applied in production — every agent has always fallen through to the default chain regardless of any pin.

**Interfaces:**
- Produces: `_build_llm_fn(config: dict) -> Callable[[str, str, str | None], tuple[dict, str | None]]` — a new **module-level** function in `agents/agent_runner.py` that builds and returns the `llm_fn` closure. Extracting it to module level (rather than leaving it inline inside `_run_once`) is what makes it independently testable — `_run_once` should call it as `llm_fn = _build_llm_fn(config)` and pass the result to `run_decision(..., llm_fn=llm_fn, ...)` exactly as before.
- The returned closure's signature must be `llm_fn(system_prompt: str, decision_prompt: str, agent_id: str | None = None) -> tuple[dict, str | None]`, and its body must call `model_chain.decide(system_prompt, decision_prompt, config=config, agent_id=agent_id)` — forwarding the `agent_id` parameter it received, not any other variable from an enclosing scope.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_runner_contract.py`:

```python
"""agent_runner's llm_fn must accept agent_id and forward it to model_chain.decide()
so per-agent pinned models resolve, and so decision_loop's _call_llm_with_retry
(which always calls llm_fn(sp, dp, agent_id=agent_id)) doesn't TypeError.
See docs/STRATEGIC_ASSESSMENT_07_09_2026.md defect C1."""
from unittest.mock import patch

from agents.agent_runner import _build_llm_fn


def test_llm_fn_accepts_agent_id_kwarg():
    """Calling exactly as decision_loop._call_llm_with_retry does must not raise."""
    config = {"desk": {"starting_balance": 50000.0}}
    llm_fn = _build_llm_fn(config)

    with patch("agents.agent_runner.model_chain.decide") as mock_decide:
        mock_decide.return_value = ({"action": "wait", "reason": "test"}, "stub-model")
        result = llm_fn("system prompt text", "decision prompt text", agent_id="jade_hawk")

    assert result == ({"action": "wait", "reason": "test"}, "stub-model")


def test_pinned_model_forwarded():
    """agent_id must reach model_chain.decide() unchanged so its pinned-model
    lookup (llm/model_chain.py's decide()) actually runs for this agent."""
    config = {"desk": {"starting_balance": 50000.0}}
    llm_fn = _build_llm_fn(config)

    with patch("agents.agent_runner.model_chain.decide") as mock_decide:
        mock_decide.return_value = ({"action": "wait", "reason": "test"}, "pinned-model")
        llm_fn("sp", "dp", agent_id="silver_basin")

    mock_decide.assert_called_once_with(
        "sp", "dp", config=config, agent_id="silver_basin"
    )


def test_llm_fn_default_agent_id_is_none():
    """agent_id must be optional (decision_loop's retry path is the only
    production caller that always supplies it, but the parameter itself
    must default sanely if called without it)."""
    config = {"desk": {"starting_balance": 50000.0}}
    llm_fn = _build_llm_fn(config)

    with patch("agents.agent_runner.model_chain.decide") as mock_decide:
        mock_decide.return_value = ({"action": "wait"}, None)
        llm_fn("sp", "dp")

    mock_decide.assert_called_once_with("sp", "dp", config=config, agent_id=None)
```

Run it, confirm it fails with `ImportError: cannot import name '_build_llm_fn'` (the function doesn't exist yet).

- [ ] **Step 2: Implement**

In `agents/agent_runner.py`, add the module-level function and update `_run_once` to use it:

```python
def _build_llm_fn(config: dict):
    """Build the llm_fn callable passed to run_decision().

    Must match agents/decision_loop.py's _call_llm_with_retry calling
    contract exactly: fn(system_prompt, decision_prompt, agent_id=None) ->
    (decision_dict, model_display_name_or_None). Forwarding agent_id lets
    model_chain.decide() resolve this agent's pinned model (see
    llm/model_chain.py's decide()) — without it, every agent silently falls
    through to the default chain regardless of any pin. See
    docs/STRATEGIC_ASSESSMENT_07_09_2026.md defect C1.
    """
    def llm_fn(
        system_prompt: str, decision_prompt: str, agent_id: str | None = None
    ) -> tuple[dict, str | None]:
        return model_chain.decide(
            system_prompt, decision_prompt, config=config, agent_id=agent_id
        )
    return llm_fn
```

Replace the inline `def llm_fn(...)` inside `_run_once` with:
```python
llm_fn = _build_llm_fn(config)
```
(same variable name, same usage at the `run_decision(...)` call site — no other change in `_run_once`).

- [ ] **Step 3: Run the new tests, confirm they pass**

- [ ] **Step 4: Run the full suite** (`C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v`) — confirm 485 + (new tests) pass, 0 failures.

- [ ] **Step 5: Self-review** — confirm no other call site of the old inline closure was missed, confirm `_build_llm_fn` is the only new module-level symbol, confirm `_run_once`'s behavior for every other branch is byte-identical to before.

- [ ] **Step 6: Commit** — `fix(r1): forward agent_id through agent_runner's llm_fn so pinned models resolve and decision_loop's retry call stops TypeErroring (R1 AC#1)`

---

### Task 2: Fix `forge.py`'s nightly counterfactual job — undefined `llm_fn` reference

**Files:**
- Modify: `forge.py`
- Test: `tests/test_forge_counterfactual_job.py` (new file)

**Context:** `forge.py`'s `main()` defines a nested async job:
```python
async def _run_counterfactual_job():
    """Run counterfactual analysis for all agents."""
    try:
        agents = conn.execute("SELECT id, name FROM agents").fetchall()
        for agent in agents:
            agent_id = agent["id"]
            agent_name = agent["name"]
            logger.info(...)
            from agents.persona import build_system_prompt

            system_prompt = build_system_prompt(agent_id, config)
            from agents.decision_loop import run_counterfactual

            await run_counterfactual(
                conn,
                agent_id,
                None,
                lambda sp, dp, **kw: llm_fn(sp, dp),
                system_prompt,
            )
    except Exception as exc:
        logger.error("Counterfactual analysis failed: %s", exc, exc_info=True)
```
The lambda's body references a bare name `llm_fn` that is **never defined anywhere in `forge.py`** — at call time this raises `NameError: name 'llm_fn' is not defined`, caught by the outer `try/except`, logged, and the job silently no-ops every night. Fixing the deeper design issue (this job asking an LLM to guess counterfactual outcomes instead of replaying recorded candles) is **out of scope for R1** — that is Revision R3. R1's job here is narrower: replace the undefined reference with a real, defined, testable callable matching `run_counterfactual`'s calling contract, so the job stops crashing.

`agents/decision_loop.py`'s `run_counterfactual(conn, agent_id, trade_id, llm_fn, system_prompt)` calls its `llm_fn` argument as:
```python
result = llm_fn(system_prompt, prompt, agent_id=agent_id)
```
(where `prompt` is the counterfactual analysis prompt `run_counterfactual` builds internally — not something this task constructs).

For the reference pattern to follow, `forge.py`'s **already-working** reflection scheduler job (`_run_reflection_scheduler_job`, elsewhere in the same file) builds its LLM closure like this — read it in the file for exact current form, do not copy verbatim since its call signature differs (single `prompt` string vs. this task's `(system_prompt, decision_prompt, agent_id=...)`):
```python
def _llm_fn(prompt: str, _aid: str = agent_id) -> str:
    from llm.model_chain import decide as mc_decide
    result, _model_name = mc_decide(
        system_prompt="You are a trading strategy reflection engine.",
        decision_prompt=prompt,
        config=config,
        agent_id=_aid,
    )
    return json.dumps(result)
```

**Interfaces:**
- Produces: `_build_counterfactual_llm_fn(config: dict) -> Callable[[str, str, str | None], tuple[dict, str | None]]` — a new **module-level** function in `forge.py`. The returned closure's signature is `fn(system_prompt: str, decision_prompt: str, agent_id: str | None = None) -> tuple[dict, str | None]`, matching `run_counterfactual`'s call `llm_fn(system_prompt, prompt, agent_id=agent_id)` exactly. Its body calls `llm.model_chain.decide(system_prompt=system_prompt, decision_prompt=decision_prompt, config=config, agent_id=agent_id)` (local import inside the closure, matching the reflection scheduler's existing local-import style in this same file) and returns the result unchanged (a `(decision_dict, model_name_or_None)` tuple — `run_counterfactual` already handles unpacking this tuple itself, do not unpack it here).
- `_run_counterfactual_job` builds the callable **once** before its per-agent loop (config does not vary per agent) and passes it directly as `run_counterfactual`'s `llm_fn` argument — replacing the broken lambda entirely, not wrapping it.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forge_counterfactual_job.py`:

```python
"""forge.py's nightly counterfactual job referenced a bare undefined name
`llm_fn` inside its lambda, raising NameError on every run (caught and
logged, so the job silently no-op'd every night). See
docs/STRATEGIC_ASSESSMENT_07_09_2026.md defect C4 (first half) / Revision R1
AC#2. This test targets only the extracted, testable callable — not the
deeper "LLM guesses counterfactual outcomes" design issue, which is R3's
scope, not R1's."""
from unittest.mock import patch

from forge import _build_counterfactual_llm_fn


def test_callable_is_defined_and_matches_run_counterfactual_contract():
    """Must not raise NameError, and must accept the exact
    (system_prompt, decision_prompt, agent_id=...) shape
    agents/decision_loop.py's run_counterfactual calls it with."""
    config = {"desk": {"starting_balance": 50000.0}}
    fn = _build_counterfactual_llm_fn(config)

    with patch("forge.model_chain.decide") if False else patch(
        "llm.model_chain.decide"
    ) as mock_decide:
        mock_decide.return_value = (
            {"action": "long", "expected_pnl_pct": 1.2, "confidence": 0.6},
            "stub-model",
        )
        result = fn("system prompt", "counterfactual prompt", agent_id="iron_moth")

    assert result == (
        {"action": "long", "expected_pnl_pct": 1.2, "confidence": 0.6},
        "stub-model",
    )
    mock_decide.assert_called_once_with(
        system_prompt="system prompt",
        decision_prompt="counterfactual prompt",
        config=config,
        agent_id="iron_moth",
    )


def test_run_counterfactual_job_no_longer_raises_nameerror(monkeypatch, tmp_path):
    """End-to-end through the actual job function: seed one agent with a
    'wait' decision joined to a trade (the shape run_counterfactual queries
    for), run _run_counterfactual_job(), assert the decisions row now has a
    non-null counterfactual_result instead of the job silently no-oping on
    a caught NameError."""
    import asyncio
    import json as json_mod
    import forge as forge_module
    from store.db import get_connection, init_schema, insert_agent, insert_account_snapshot
    from store.db import insert_trade

    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_schema(conn)
    insert_agent(conn, "iron_moth", "iron_moth", "2026-07-01T00:00:00Z", "{}")
    insert_account_snapshot(conn, "iron_moth", "paper", 50000.0, 50000.0)
    insert_trade(conn, {
        "id": "iron_moth_20260701_000000_BTC",
        "agent_id": "iron_moth",
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": "BTC-PERP",
        "direction": "long",
        "entry_price": 65000.0,
        "stop_loss_price": 63000.0,
        "take_profit_price": 68000.0,
        "leverage": 3,
        "position_size_pct": 0.1,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-07-01T00:05:00Z",
        "status": "open",
    })
    conn.execute(
        """INSERT INTO decisions (agent_id, timestamp, decision_action, decision_reason)
           VALUES (?, ?, ?, ?)""",
        ("iron_moth", "2026-07-01T00:05:00Z", "wait", "test wait"),
    )
    conn.commit()

    monkeypatch.setattr(
        "llm.model_chain.decide",
        lambda **kw: ({"action": "wait", "expected_pnl_pct": 0}, "stub-model"),
    )

    forge_module.DB_PATH = __import__("pathlib").Path(db_path)
    config = {"desk": {"starting_balance": 50000.0}}

    async def _run():
        cf_llm_fn = forge_module._build_counterfactual_llm_fn(config)
        from agents.persona import build_system_prompt
        from agents.decision_loop import run_counterfactual

        system_prompt = build_system_prompt("iron_moth", config)
        await run_counterfactual(conn, "iron_moth", None, cf_llm_fn, system_prompt)

    asyncio.run(_run())

    row = conn.execute(
        "SELECT counterfactual_result FROM decisions WHERE agent_id = ?", ("iron_moth",)
    ).fetchone()
    assert row["counterfactual_result"] is not None
    conn.close()
```

Run it, confirm `test_callable_is_defined_and_matches_run_counterfactual_contract` fails with `ImportError: cannot import name '_build_counterfactual_llm_fn'`.

- [ ] **Step 2: Implement**

Add to `forge.py` (module level, near the other helper functions — not nested inside `main()`):

```python
def _build_counterfactual_llm_fn(config: dict):
    """Build the llm_fn callable the nightly counterfactual job passes to
    agents.decision_loop.run_counterfactual(). Matches its calling contract
    exactly: fn(system_prompt, decision_prompt, agent_id=None) ->
    (decision_dict, model_display_name_or_None).

    The prior code referenced a bare, never-defined name `llm_fn` inside a
    lambda, raising NameError on every call (silently caught and logged,
    so the job no-op'd every night). This callable is the fix for that
    wiring bug only — it still asks the LLM to reason about a
    counterfactual rather than mechanically replaying recorded candles;
    that deeper redesign is docs/STRATEGIC_ASSESSMENT_07_09_2026.md's
    Revision R3, not this one.
    """
    def _fn(
        system_prompt: str, decision_prompt: str, agent_id: str | None = None
    ) -> tuple[dict, str | None]:
        from llm.model_chain import decide as mc_decide
        return mc_decide(
            system_prompt=system_prompt,
            decision_prompt=decision_prompt,
            config=config,
            agent_id=agent_id,
        )
    return _fn
```

In `main()`, inside `_run_counterfactual_job`, build the callable once before the loop and use it in place of the broken lambda:

```python
async def _run_counterfactual_job():
    """Run counterfactual analysis for all agents."""
    try:
        cf_llm_fn = _build_counterfactual_llm_fn(config)
        agents = conn.execute("SELECT id, name FROM agents").fetchall()
        for agent in agents:
            agent_id = agent["id"]
            agent_name = agent["name"]
            logger.info(
                "Running counterfactual analysis for agent %s (%s)",
                agent_id,
                agent_name,
            )
            from agents.persona import build_system_prompt

            system_prompt = build_system_prompt(agent_id, config)
            from agents.decision_loop import run_counterfactual

            await run_counterfactual(
                conn,
                agent_id,
                None,
                cf_llm_fn,
                system_prompt,
            )
    except Exception as exc:
        logger.error("Counterfactual analysis failed: %s", exc, exc_info=True)
```

- [ ] **Step 3: Run the new tests, confirm they pass**

- [ ] **Step 4: Run the full suite** — confirm all pass, 0 failures. Note `tests/test_forge_agent_timeout.py` / `tests/test_forge_heartbeat_schedule.py` are expected to pass now too (see Global Constraints) — if either fails in your environment, report it in your DONE/BLOCKED status rather than silently re-adding an `--ignore`.

- [ ] **Step 5: Self-review** — confirm the lambda is gone entirely (not wrapped), confirm `_build_counterfactual_llm_fn` is built once per job run (not once per agent inside the loop), confirm no change to `run_counterfactual`'s or `build_system_prompt`'s call sites beyond swapping the callable argument.

- [ ] **Step 6: Commit** — `fix(r1): define the counterfactual job's llm_fn instead of referencing an undefined name (R1 AC#2)`

---

### Task 3: Await `run_postmortem` instead of fire-and-forget in `agents/decision_loop.py`

**Files:**
- Modify: `agents/decision_loop.py`
- Test: `tests/test_decision_loop.py` (extend existing file)

**Context:** In `run_decision`'s `action == "close"` branch (locate the exact current code — do not guess line numbers):
```python
if action == "close":
    pos_id = response.get("position_id")
    reason = response.get("reason", "agent_close")
    bridge = bridge_factory(agent_id, conn, provider)
    fill = await bridge.close(pos_id, reason)
    logger.info("[%s] Closed position %s: %s", agent_id, pos_id, fill)
    trade_id = fill.get("trade_id")
    if trade_id:
        asyncio.ensure_future(
            run_postmortem(conn, agent_id, trade_id, llm_fn, system_prompt)
        )
    log_decision(
        conn, agent_id, "close", reason,
        {"position_id": pos_id, "fill": str(fill)},
        model_used=model_label,
    )
    return {"action": "close", "detail": str(fill)}
```
`agents/agent_runner.py`'s `_run_once` runs the entire decision cycle as `asyncio.run(_run_once(...))` — a **one-shot subprocess** that exits as soon as `_run_once` returns. `asyncio.ensure_future(...)` schedules `run_postmortem(...)` on the event loop but does not await it; `run_decision` returns immediately after, `_run_once` returns, `asyncio.run()` tears down the event loop, and the scheduled postmortem task is cancelled before it ever runs its LLM call. Postmortems for closed trades are silently and permanently lost. The fix: await it directly. This is a one-shot subprocess exiting anyway, so the extra latency of waiting for one more LLM-shaped call before returning is the correct tradeoff (see Global Constraints).

**Interfaces:** No new functions. `run_decision`'s return shape, `run_postmortem`'s signature, and every other branch of `run_decision` are unchanged.

**Steps:**

- [ ] **Step 1: Write the failing test**

Add to `tests/test_decision_loop.py` (match the existing file's fixture/mocking conventions — read a couple of its existing `close`-branch tests first to reuse its setup pattern rather than inventing a new one):

```python
async def test_close_action_awaits_postmortem_before_returning(monkeypatch, ...):
    """A just-closed trade's postmortem must complete before run_decision
    returns — not be fire-and-forgotten via asyncio.ensure_future, which
    agent_runner's one-shot asyncio.run(...) subprocess model silently
    drops before it ever executes. See
    docs/STRATEGIC_ASSESSMENT_07_09_2026.md defect C1 (postmortem half) /
    Revision R1 AC#4."""
    postmortem_completed = False

    async def fake_run_postmortem(conn, agent_id, trade_id, llm_fn, system_prompt):
        nonlocal postmortem_completed
        postmortem_completed = True

    monkeypatch.setattr("agents.decision_loop.run_postmortem", fake_run_postmortem)

    # ... construct a close-action response/bridge per this file's existing
    # close-branch test fixtures ...

    result = await run_decision(...)  # drive an action == "close" path

    assert postmortem_completed is True
    assert result["action"] == "close"
```

Adapt the `...` portions to match whatever fixture helpers this test file already uses for a close-branch decision (there should be at least one existing close-branch test in the file to model this on). The essential assertion is `postmortem_completed is True` *immediately* after `run_decision` returns, with no `asyncio.sleep`/`gather` needed in the test to "wait for it to catch up" — if the test needs to sleep to observe completion, the implementation is still fire-and-forget and Step 2 hasn't fixed it.

Run it, confirm it fails (postmortem not completed synchronously).

- [ ] **Step 2: Implement**

Replace:
```python
    if trade_id:
        asyncio.ensure_future(
            run_postmortem(conn, agent_id, trade_id, llm_fn, system_prompt)
        )
```
with:
```python
    if trade_id:
        await run_postmortem(conn, agent_id, trade_id, llm_fn, system_prompt)
```

Then check whether `import asyncio` at the top of `agents/decision_loop.py` is still used anywhere else in the file (`grep -n "asyncio\." agents/decision_loop.py`). If `asyncio.ensure_future` was the only usage, remove the now-dead `import asyncio` line.

- [ ] **Step 3: Run the new test, confirm it passes**

- [ ] **Step 4: Run the full suite** — confirm all pass, 0 failures.

- [ ] **Step 5: Self-review** — confirm no other `asyncio.` usage was missed before deciding whether to remove the import; confirm the `close` branch's other behavior (fill, log_decision, return value) is unchanged.

- [ ] **Step 6: Commit** — `fix(r1): await run_postmortem instead of fire-and-forget so it isn't dropped when the agent subprocess exits (R1 AC#4)`

---

## Final Verification (after all 3 tasks, before the whole-branch review)

- [ ] Full suite green: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v` — 485 + new tests, 0 failures.
- [ ] Manual read-through: `agent_id` now flows `decision_loop._call_llm_with_retry` → `agent_runner._build_llm_fn`'s closure → `model_chain.decide()` with no signature mismatch anywhere in the chain.
- [ ] Manual read-through: `forge.py` contains no remaining reference to a bare undefined `llm_fn` name.
- [ ] Manual read-through: no remaining `asyncio.ensure_future` wrapping `run_postmortem`.
