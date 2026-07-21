# Risk-Gate Visibility & Ollama Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every currently-open `entry_disables` row visible in the UI with its reason/who/when (and a one-click re-enable), stop the disable endpoint from silently stacking duplicate rows, and reduce the chance of the local-Ollama-pinned agents (`copper_vane`, `steel_crane`, `onyx_heron`) failing a decision cycle with "unavailable".

**Architecture:** No new tables. `entry_disables` (schema unchanged) is already the single source of truth read live by `RiskOfficer.entry_gate_status()` on every decision (`agents/decision_loop.py:491`). We add: (1) idempotency to the existing `/api/exec/disable-entries/{agent_id}` endpoint so repeated calls hold one open row instead of stacking, (2) a read-only helper that both web routes reuse to fetch the open disable row for an agent, (3) template badges + a re-enable button wired to the existing (already-implemented, currently UI-orphaned) `/api/exec/enable-entries/{agent_id}` endpoint, (4) an informational (non-invariant-checked) field on `RiskOfficerOutput` so *any* open disable — not just the officer's own throttle — shows up in the risk officer's own `reason` string and server logs, and (5) an explicit Ollama `keep_alive` + duration logging to reduce/diagnose local-model unavailability.

**Tech Stack:** FastAPI (`web/app.py`), Jinja2 templates, SQLite (`data/forge.db`), pytest.

## Global Constraints

- Never touch `RiskOfficerOutput.entry_disabled_agents` semantics — it is covered by `validate_risk_officer_output`'s reduce-only invariant (`meta/risk_officer.py:130`) and is deliberately scoped to `disabled_by='risk_officer'` rows only (`meta/risk_officer.py:92-95`). Mixing human-set disables into it would make a human's manual re-enable (via the web endpoint, which bypasses the officer's validator) look like an illegal "risk decrease" on the next cycle and raise `RiskViolation`. Any new visibility field must be additive and NOT invariant-checked.
- `RiskOfficer.enable_entry()` must keep only lifting `disabled_by='risk_officer'` rows (`meta/risk_officer.py:250-258`) — that restriction is intentional (officer can never override a human-set stop). Do not weaken it.
- Config convention: no invented default numbers; read thresholds from `config.get("desk", {})` per `CLAUDE.md`.
- Run tests with `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py` (apscheduler not installed in this Python env).

---

## Root cause recap (already diagnosed, informs this plan)

`web/app.py`'s `/api/exec/disable-entries/{agent_id}` route unconditionally `INSERT`s a new `entry_disables` row every call, with no idempotency check. Something called it repeatedly (once per agent, every ~5 min) for ~2.5 days in 2026-07-12→07-15, producing 5,225 open rows, all `disabled_by='human'`. Because `RiskOfficer.enable_entry()` can only lift its own (`disabled_by='risk_officer'`) rows, these were permanently stuck — invisible in the UI (nothing reads `entry_disables` or `app.state.risk_officer_output` in any template) until manually cleared via raw SQL on 2026-07-20. This plan closes the two real gaps: (a) the endpoint can stack unbounded duplicate/stuck rows, and (b) there is currently zero UI surfacing of "this agent can't trade and here's why."

---

### Task 1: Idempotent disable-entries endpoint

**Files:**
- Modify: `web/app.py:1344-1357` (`exec_disable_entries`)
- Test: `tests/test_web_actions.py`

**Interfaces:**
- Produces: `exec_disable_entries` no longer inserts a second open row for an agent that already has one — repeated calls are a no-op (200 OK, `{"ok": True, "already_disabled": true}`), so a spamming caller (script, retried request) can never again silently accumulate thousands of rows.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_actions.py` near the existing disable-entries tests (around line 515):

```python
def test_disable_entries_is_idempotent(conn):
    """Repeated disable calls must not stack duplicate open rows — this is
    the exact mechanism that produced 5,225 stuck entry_disables rows
    2026-07-12..07-15 (disabled_by='human', no way for the officer to ever
    auto-clear them)."""
    client = _client(conn)
    for _ in range(5):
        r = client.post(f"/api/exec/disable-entries/{AGENT_ID}?reason=spam_test")
        assert r.status_code == 200

    open_rows = conn.execute(
        "SELECT COUNT(*) FROM entry_disables WHERE agent_id = ? AND enabled_at IS NULL",
        (AGENT_ID,),
    ).fetchone()[0]
    assert open_rows == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_web_actions.py::test_disable_entries_is_idempotent -v`
Expected: FAIL — `assert 5 == 1`

- [ ] **Step 3: Implement idempotency**

Replace `web/app.py:1344-1357`:

```python
@app.post("/api/exec/disable-entries/{agent_id}")
async def exec_disable_entries(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    agent = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    existing = conn.execute(
        "SELECT id FROM entry_disables WHERE agent_id = ? AND enabled_at IS NULL",
        (agent_id,),
    ).fetchone()
    if existing:
        return {"ok": True, "already_disabled": True}

    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason, enabled_at) VALUES (?, 'human', ?, ?, NULL)",
        (agent_id, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), reason),
    )
    conn.commit()
    _audit(conn, "disable_entries", agent_id, reason)
    return {"ok": True, "already_disabled": False}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_web_actions.py::test_disable_entries_is_idempotent -v`
Expected: PASS. Also re-run the pre-existing disable/enable tests (`-k disable_entries or enable_entries`) to confirm no regression.

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_web_actions.py
git commit -m "fix(web): make disable-entries endpoint idempotent"
```

---

### Task 2: RiskOfficer surfaces ALL open entry_disables (not just its own)

**Files:**
- Modify: `meta/risk_officer.py` (`RiskOfficerOutput` dataclass, `run_risk_officer_job`)
- Test: `tests/test_risk_officer.py`

**Interfaces:**
- Consumes: `entry_disables` table (existing schema: `agent_id, disabled_by, disabled_at, reason, enabled_at`).
- Produces: new field `RiskOfficerOutput.other_open_disables: list[dict]` — one dict per currently-open row NOT owned by `disabled_by='risk_officer'` (i.e. human or any other actor), shape `{"agent_id": str, "reason": str, "disabled_by": str, "disabled_at": str}`. This field is purely informational — it is NOT read by `validate_risk_officer_output` (that function only ever touches `entry_disabled_agents` and `regime`, unchanged). `run_risk_officer_job`'s `reason` string appends a summary of these rows so they show up in `forge.py`'s existing `if risk_output.reason != "all clear": logger.warning(...)` line — closing the "silent for 8 days" gap at the log level, ahead of the UI work in Task 3/4.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_risk_officer.py` near `test_run_risk_officer_job_clear_desk` (around line 440):

```python
def test_run_risk_officer_job_surfaces_human_disables(conn):
    """A human/other-originated entry_disables row (the exact shape that
    got stuck invisibly for 8 days) must show up in the output even though
    the officer did not create it and cannot clear it."""
    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason) "
        "VALUES ('agent_a', 'human', '2026-07-12T18:11:10Z', 'Entry blocked by risk check')"
    )
    conn.commit()
    config = {"desk": {"max_gross_exposure_mult": 3.0}}
    output = run_risk_officer_job(conn, config)

    assert "agent_a" not in output.entry_disabled_agents  # invariant field untouched
    assert len(output.other_open_disables) == 1
    assert output.other_open_disables[0]["agent_id"] == "agent_a"
    assert output.other_open_disables[0]["disabled_by"] == "human"
    assert "agent_a" in output.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_risk_officer.py::test_run_risk_officer_job_surfaces_human_disables -v`
Expected: FAIL — `AttributeError: 'RiskOfficerOutput' object has no attribute 'other_open_disables'`

- [ ] **Step 3: Implement**

In `meta/risk_officer.py`, extend the dataclass (around line 66-78):

```python
@dataclass(frozen=True)
class RiskOfficerOutput:
    """Immutable snapshot of a single risk-officer cycle's decisions.

    Returned by ``run_risk_officer_job`` and consumed by the risk gate
    (risk/gate.py) to block entries for disabled agents.  The reduce-only
    validator (``validate_risk_officer_output``) compares two consecutive
    snapshots to ensure the risk officer never increases risk.
    """

    entry_disabled_agents: list[str] = field(default_factory=list)
    reason: str = ""
    regime: str = ""
    # Informational only — NOT covered by validate_risk_officer_output's
    # reduce-only invariant. Rows here are entry_disables the officer did
    # not create (disabled_by != 'risk_officer') and therefore can never
    # auto-clear (RiskOfficer.enable_entry only lifts its own rows). This
    # field exists purely so a stuck human/other disable is visible in
    # logs/UI instead of silent — see the 2026-07-12..07-15 incident where
    # 5,225 such rows blocked every agent for 8 days unnoticed.
    other_open_disables: list[dict] = field(default_factory=list)
```

Then in `run_risk_officer_job` (around line 89-127), add the query and fold it into `reason`:

```python
def run_risk_officer_job(conn, config: dict | None = None) -> RiskOfficerOutput:
    officer = RiskOfficer(conn, config)
    report = officer.run_cycle()

    disabled_rows = conn.execute(
        """SELECT DISTINCT agent_id FROM entry_disables
           WHERE enabled_at IS NULL AND disabled_by = 'risk_officer'"""
    ).fetchall()
    entry_disabled_agents = sorted(row["agent_id"] for row in disabled_rows)

    other_rows = conn.execute(
        """SELECT agent_id, reason, disabled_by, disabled_at FROM entry_disables
           WHERE enabled_at IS NULL AND disabled_by != 'risk_officer'
           ORDER BY disabled_at"""
    ).fetchall()
    other_open_disables = [dict(r) for r in other_rows]

    desk_kill = report.get("desk_kill_switch", False)
    blackout = report.get("event_blackout")
    if desk_kill or blackout is not None:
        all_agent_rows = conn.execute(
            """SELECT id FROM agents
               WHERE status IN ('rookie', 'active')"""
        ).fetchall()
        all_agent_ids = {row["id"] for row in all_agent_rows}
        entry_disabled_agents = sorted(all_agent_ids | set(entry_disabled_agents))

    reasons = []
    if desk_kill:
        reasons.append("desk kill switch active")
    if blackout is not None:
        reasons.append(f"event blackout ({blackout.get('name', 'unknown')})")
    if report.get("desk_daily_loss_exceeded"):
        reasons.append("desk daily loss exceeded")
    throttled = report.get("gross_exposure_throttled_agents", [])
    if throttled:
        reasons.append(f"gross exposure throttle: {', '.join(throttled)}")
    if other_open_disables:
        agents_blocked = ", ".join(sorted({r["agent_id"] for r in other_open_disables}))
        reasons.append(f"non-officer entry disables open for: {agents_blocked}")
    reason = "; ".join(reasons) if reasons else "all clear"

    memo = report.get("regime_memo") or {}
    regime = memo.get("regime_tag", "")

    return RiskOfficerOutput(
        entry_disabled_agents=entry_disabled_agents,
        reason=reason,
        regime=regime,
        other_open_disables=other_open_disables,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_risk_officer.py -v`
Expected: all PASS, including the new test and the pre-existing `test_run_risk_officer_job_clear_desk` / `test_risk_officer_output_defaults` (the new field defaults to `[]`, so `RiskOfficerOutput()` with no args still works).

- [ ] **Step 5: Commit**

```bash
git add meta/risk_officer.py tests/test_risk_officer.py
git commit -m "feat(risk): surface non-officer entry_disables in risk officer output"
```

---

### Task 3: Backend helper + route wiring for per-agent disable status

**Files:**
- Modify: `web/app.py` (add helper function, wire into `overview()` and `agent_detail()`)
- Test: `tests/test_web_actions.py`

**Interfaces:**
- Produces: `_entry_disable_status(conn, agent_id) -> dict | None` in `web/app.py`, returning `{"reason": str, "disabled_by": str, "disabled_at": str}` for the agent's open `entry_disables` row (any `disabled_by`), or `None` if the gate is open. Both `overview()`'s per-agent dict (`a["entry_disable"]`) and `agent_detail()`'s template context (`entry_disable`) use it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_actions.py`:

```python
def test_overview_shows_entry_disable_reason(conn):
    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason) "
        f"VALUES ('{AGENT_ID}', 'human', '2026-07-15T06:26:40Z', 'Entry blocked by risk check')"
    )
    conn.commit()
    r = _client(conn).get("/")
    assert r.status_code == 200
    assert "Entry blocked by risk check" in r.text


def test_agent_detail_shows_entry_disable_reason(conn):
    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason) "
        f"VALUES ('{AGENT_ID}', 'human', '2026-07-15T06:26:40Z', 'Entry blocked by risk check')"
    )
    conn.commit()
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "Entry blocked by risk check" in r.text
    assert "Enable Entries" in r.text
```

Check `tests/test_web_actions.py`'s existing fixtures for how `_client(conn)` builds the app and what `AGENT_ID` is seeded as before writing these — reuse the same seeded agent, don't invent a new one.

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_web_actions.py::test_overview_shows_entry_disable_reason tests/test_web_actions.py::test_agent_detail_shows_entry_disable_reason -v`
Expected: FAIL — reason text not in response body (templates don't render it yet).

- [ ] **Step 3: Implement the helper and wire it into both routes**

Add near the top-level helpers in `web/app.py` (close to `_audit`, around line 1195-1200):

```python
def _entry_disable_status(conn, agent_id: str) -> dict | None:
    """Currently-open entry_disables row for this agent, any disabled_by.

    Mirrors the live gate RiskOfficer.entry_gate_status() reads at decision
    time (agents/decision_loop.py:491) — this is read-only and does not
    affect gating, it only lets the UI show what the gate is already
    enforcing. Returns None when the gate is open.
    """
    row = conn.execute(
        """SELECT reason, disabled_by, disabled_at FROM entry_disables
           WHERE agent_id = ? AND enabled_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()
    return dict(row) if row else None
```

In `overview()`, inside the per-agent loop (`web/app.py:229-257`), add one line to the appended dict:

```python
        agents.append(
            {
                "name": aid,
                "status": agent["status"],
                "trades_count": metrics["closed_trades"],
                "win_rate": metrics["win_rate"],
                "profit_factor": metrics["profit_factor"]
                if metrics["profit_factor"] != float("inf")
                else 0.0,
                "sharpe": metrics["sharpe"],
                "weekly_return": metrics.get("last_7d_return", 0.0),
                "max_drawdown": (peak - bal) / peak if peak > 0 else 0.0,
                "open_positions_count": pos_count,
                "last_model_used": _resolve_model_used(conn, aid, agent.get("last_model_used")),
                "entry_disable": _entry_disable_status(conn, aid),
            }
        )
```

In `agent_detail()` (`web/app.py:948-967`), add to the template context:

```python
    return templates.TemplateResponse(
        "agent_detail.html",
        {
            "request": request,
            "agent": agent,
            "account": account_dict,
            "max_drawdown": max_dd,
            "metrics": metrics,
            "open_positions": [dict(p) for p in open_positions],
            "trade_history": [dict(t) for t in trade_history],
            "thesis_version": thesis_version,
            "thesis_text": thesis_text,
            "data_source": data_source,
            "exchange_ok": exchange_ok,
            "spec_history": spec_history,
            "spec_diff": spec_diff,
            "calibration": calibration,
            "reflection_cycles": reflection_cycles,
            "entry_disable": _entry_disable_status(conn, aid),
        },
    )
```

- [ ] **Step 4: Run test to verify it still fails the same way (templates come in Task 4)**

Run the same pytest command as Step 2 — still FAIL at this point (backend supplies the data, templates don't render it yet). This confirms the failure moved from "missing data" to "missing template markup", not a false pass.

- [ ] **Step 5: Commit**

```bash
git add web/app.py
git commit -m "feat(web): thread entry-disable status into overview/agent_detail routes"
```

---

### Task 4: UI badges, detail panel, and re-enable button

**Files:**
- Modify: `web/templates/overview.html`
- Modify: `web/templates/agent_detail.html`
- Modify: `web/static/forge.css` (badge color)
- Test: `tests/test_web_actions.py` (Task 3's two tests now pass)

**Interfaces:**
- Consumes: `a.entry_disable` (overview.html, per leaderboard row) and `entry_disable` (agent_detail.html, top-level context) — both `dict | None` as produced by Task 3.
- Produces: nothing consumed by later tasks — this is the leaf UI task.

- [ ] **Step 1: Add the badge CSS**

In `web/static/forge.css`, near the existing `.badge-no-model` rule, add:

```css
.badge-entry-disabled {
  background: #7a1f1f;
  color: #fff;
}
```

- [ ] **Step 2: Overview leaderboard — badge + link**

In `web/templates/overview.html`, the Status cell (line 98) becomes:

```html
      <td>
        <span class="badge badge-{{ a.status }}">{{ a.status.upper() }}</span>
        {% if a.entry_disable %}
        <a href="/agents/{{ a.name }}#risk-gate" class="badge badge-entry-disabled" title="{{ a.entry_disable.reason }}">ENTRY DISABLED</a>
        {% endif %}
      </td>
```

- [ ] **Step 3: Agent detail — full panel + re-enable button**

In `web/templates/agent_detail.html`, right after the header badges block (after line 17, before the next section), add:

```html
{% if entry_disable %}
<div id="risk-gate" class="panel panel-warning">
  <h3 class="section-header">Entry Disabled</h3>
  <p><strong>Reason:</strong> {{ entry_disable.reason or 'no reason recorded' }}</p>
  <p><strong>Disabled by:</strong> {{ entry_disable.disabled_by }}</p>
  <p><strong>Disabled at:</strong> {{ entry_disable.disabled_at }}</p>
  <button class="btn btn-sm btn-primary" onclick="openModal('Enable Entries', 'Allow {{ agent.id }} to open new positions again.', '/api/exec/enable-entries/{{ agent.id }}', 'POST')">Enable Entries</button>
</div>
{% endif %}
```

Check `web/templates/agent_detail.html` and `web/static/forge.css` for the exact `.panel` / `.panel-warning` class names already in use elsewhere in the file before adding a new one — reuse whatever the existing warning/alert box convention is instead of inventing a new class if one already exists.

- [ ] **Step 4: Run the Task 3 tests to verify they now pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_web_actions.py::test_overview_shows_entry_disable_reason tests/test_web_actions.py::test_agent_detail_shows_entry_disable_reason -v`
Expected: PASS

- [ ] **Step 5: Manual verification**

Start the app (`C:\ProgramData\Anaconda3\python.exe forge.py` or however the dev server is normally started per `run` skill) and visually confirm on `data/forge.db` — insert a throwaway open `entry_disables` row for one agent, load `/` and `/agents/<name>`, confirm the badge/panel render and "Enable Entries" actually clears it, then remove the throwaway row (or use the UI button) so this doesn't leave the live desk agent disabled.

- [ ] **Step 6: Commit**

```bash
git add web/templates/overview.html web/templates/agent_detail.html web/static/forge.css
git commit -m "feat(web): show entry-disable reason + re-enable button in UI"
```

---

### Task 5: Ollama keep_alive + duration logging

**Files:**
- Modify: `llm/ollama_client.py`
- Test: `tests/test_ollama_client.py` if it exists, else add one at that path.

**Interfaces:**
- Produces: `decide()`'s payload includes an explicit `keep_alive` so the 36B model stays resident between the 5-minute heartbeat cycles (default Ollama `keep_alive` is 5m — right at the cycle boundary, so any cycle that runs slightly late pays a full model-reload before it can even start inferring). Also logs call duration on success, so a future 900s-timeout incident has data (today only the failure path logs anything).

- [ ] **Step 1: Check for an existing test file**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_ollama_client.py -v` (or `Get-ChildItem tests | Select-String -Pattern ollama` in PowerShell) to see if one exists and what fixtures/mocking it already uses (likely `respx` per `CLAUDE.md`'s testing notes). Follow its existing httpx-mocking pattern for the new test rather than introducing a different mocking approach.

- [ ] **Step 2: Write the failing test**

```python
import time
import httpx
import pytest
import respx

from llm import ollama_client


@respx.mock
@pytest.mark.asyncio
async def test_decide_sends_keep_alive():
    route = respx.post(ollama_client.OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"message": {"content": '{"action": "wait", "reason": "x"}'}})
    )
    await ollama_client.decide("sys", "prompt")
    sent_body = route.calls.last.request.content
    import json as _json
    payload = _json.loads(sent_body)
    assert payload["keep_alive"] == "30m"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_ollama_client.py::test_decide_sends_keep_alive -v`
Expected: FAIL — `KeyError: 'keep_alive'`

- [ ] **Step 4: Implement**

In `llm/ollama_client.py`, update the payload and add duration logging:

```python
async def decide(system_prompt: str, decision_prompt: str, config: dict | None = None) -> dict | None:
    model = (config or {}).get("llm_model") or MODEL
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": decision_prompt},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": bool((config or {}).get("llm_think", False)),
        "options": {"temperature": 0.7},
        # Default Ollama keep_alive is 5m — the same cadence as forge.py's
        # heartbeat cycle, so a cycle that starts a few seconds late pays a
        # full reload of this 36B model before it can even start
        # inferring. 30m keeps it resident across several cycles' worth of
        # idle time between calls.
        "keep_alive": "30m",
    }
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECS) as client:
            resp = await client.post(OLLAMA_URL, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except httpx.TimeoutException:
        logger.warning("Ollama request timed out after %ds", TIMEOUT_SECS)
        return None
    except Exception as exc:
        logger.error("Ollama request failed: %s", exc)
        return None
    logger.info("Ollama request completed in %.1fs", time.monotonic() - start)

    content = body.get("message", {}).get("content", "")
    if not content:
        logger.warning("Ollama returned empty content")
        return None

    return _extract_json(content)
```

(Add `import time` at the top of the file alongside the existing `import json` / `import logging`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_ollama_client.py -v`
Expected: PASS, including any pre-existing tests in that file.

- [ ] **Step 6: Commit**

```bash
git add llm/ollama_client.py tests/test_ollama_client.py
git commit -m "fix(llm): keep Ollama model resident across heartbeat cycles, log call duration"
```

---

## Explicitly out of scope (and why)

- **Auto-expiring human-set `entry_disables` rows.** The officer intentionally can never lift a human's disable (`meta/risk_officer.py:250-258`) — that's a safety property, not a bug. The fix for "invisible for 8 days" is visibility (Tasks 2-4), not letting the system quietly re-enable itself.
- **Cross-process locking/semaphore around the 3 Ollama-pinned agents.** They run as separate OS subprocesses (`forge.py`'s fleet cycle, `asyncio.gather` over `agent_runner.py` subprocesses), so an in-process `asyncio.Semaphore` in `llm/ollama_client.py` would do nothing — each subprocess gets its own interpreter. Ollama already serializes concurrent requests to the same loaded model server-side, so cross-process contention is not the confirmed root cause; `keep_alive` (Task 5) targets the concretely evidenced reload-cost problem without adding a new locking mechanism on unconfirmed grounds.
- **Lowering `TIMEOUT_SECS` from 900s.** `llm/ollama_client.py`'s own comment documents that 300s previously caused false timeouts under real concurrent load — reverting that without new evidence would reintroduce a known-bad state.
