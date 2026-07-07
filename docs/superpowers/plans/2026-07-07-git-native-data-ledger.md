# Git-Native Data Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the gitignored, disposable `data/forge.db` + `data/historical_data/` as Forge's system of record with a git-tracked, append-only ledger (`ledger/` + `state/`) that is committed and pushed every heartbeat cycle, so a fresh `git clone` + one rebuild command reproduces the exact last-known state of the desk.

**Architecture:** Every historically-meaningful write (decisions, market data, closed trades, account snapshots) is duplicated into a monthly-partitioned JSONL file under `ledger/{kind}/{YYYY-MM}.jsonl`, appended alongside the existing SQLite write, never replacing it — `data/forge.db` remains the fast local read/write cache. A tiny `state/current.json` snapshot (agents, open positions, balances) is rewritten and committed every cycle. A new best-effort git-sync step commits+pushes both after each heartbeat. A monthly compaction script converts closed months to Parquet. A rebuild script reconstructs `data/forge.db` from the ledger alone.

**Tech Stack:** Python 3.11, SQLite (existing), `pandas`/`pyarrow` for Parquet (already in `requirements.txt` — no new dependencies), `subprocess` for git operations, `pytest` + `pytest-asyncio` for tests.

## Global Constraints

- Python interpreter: `C:\ProgramData\Anaconda3\python.exe` (bare `python` resolves to a no-op stub on this machine — see CLAUDE.md).
- Test command: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py`
- Every new I/O path that runs in the live heartbeat/decision hot path must be **best-effort and non-blocking**: wrap in try/except, log a warning, never raise — matching the existing pattern in `market/heartbeat.py`'s `append_historical()`.
- File writes that must never be observed half-written use the atomic `write-to-.tmp-then-os.replace` pattern already established in `market/heartbeat.py`'s `write_heartbeat()`.
- No new pip dependencies — `pyarrow`, `pandas`, `pytest` are already present.
- Design reference: `docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md`.

---

### Task 1: Reset — wipe corrupted legacy data, extend `fresh_start.py`

**Files:**
- Modify: `scripts/fresh_start.py`

**Interfaces:**
- Produces: `PROJECT_ROOT`, `DB_PATH`, `SEED_AGENTS` (unchanged names, still importable) — later tasks don't depend on this file's internals.

- [ ] **Step 1: Extend the wipe to cover the pre-ledger legacy paths**

Add `import shutil` to the top of `scripts/fresh_start.py` (alongside the existing `import sys`), and insert this block immediately after the existing WAL/SHM sidecar-deletion loop (after the line `print(f"Deleted stale sidecar file: {sidecar}")`) and before the `# Initialize fresh schema` comment:

```python
    # Retire legacy pre-ledger capture -- superseded by ledger/ (see
    # docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md).
    # Wrong schema for the new system either way, so nothing here is worth
    # migrating forward.
    legacy_historical_dir = PROJECT_ROOT / "data" / "historical_data"
    if legacy_historical_dir.exists():
        shutil.rmtree(legacy_historical_dir)
        print(f"Deleted legacy historical capture: {legacy_historical_dir}")

    oi_history_path = PROJECT_ROOT / "data" / "heartbeat_oi_history.json"
    if oi_history_path.exists():
        oi_history_path.unlink()
        print(f"Deleted stale OI history baseline: {oi_history_path}")
```

- [ ] **Step 2: Run it and verify a clean reset**

Run: `C:\ProgramData\Anaconda3\python.exe scripts\fresh_start.py --yes`

Expected output ends with:
```
Done. 10 agents seeded, 10 account snapshots created.

Ready. Run: python forge.py
```

Then verify:
```bash
ls data/historical_data 2>&1          # should report "No such file or directory"
ls data/heartbeat_oi_history.json 2>&1 # should report "No such file or directory"
```

- [ ] **Step 3: Commit**

```bash
git add scripts/fresh_start.py
git commit -m "feat(ledger): extend fresh_start.py to retire pre-ledger legacy capture"
```

**Definition of done:** `python scripts/fresh_start.py --yes` deletes `data/forge.db`, `data/historical_data/`, and `data/heartbeat_oi_history.json`, then reseeds exactly the 10 `SEED_AGENTS` (each with a thesis row, not just a bare agent row) at the configured starting balance.

---

### Task 2: Ledger record writer (`store/ledger.py`)

**Files:**
- Create: `store/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces: `append_ledger_record(kind: str, record: dict, when: datetime | None = None, ledger_dir: str | None = None) -> None` — `ledger_dir` defaults to the current value of module-level `LEDGER_DIR`, resolved at call time. Later tasks (3, 4, 5) import this exact signature.
- Produces: `LEDGER_DIR = "ledger"` module constant.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ledger.py`:

```python
import json
from datetime import datetime, timezone

from store.ledger import append_ledger_record


def test_append_creates_month_partition_file(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    when = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    append_ledger_record(
        "decisions", {"agent": "sage_turtle", "action": "wait"}, when, ledger_dir
    )

    path = tmp_path / "ledger" / "decisions" / "2026-07.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"agent": "sage_turtle", "action": "wait"}


def test_append_twice_same_month_appends_two_lines(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    when = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    append_ledger_record("decisions", {"n": 1}, when, ledger_dir)
    append_ledger_record("decisions", {"n": 2}, when, ledger_dir)

    path = tmp_path / "ledger" / "decisions" / "2026-07.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(l)["n"] for l in lines] == [1, 2]


def test_append_different_months_creates_separate_files(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    append_ledger_record(
        "candles_5m", {"m": "jun"}, datetime(2026, 6, 30, tzinfo=timezone.utc), ledger_dir
    )
    append_ledger_record(
        "candles_5m", {"m": "jul"}, datetime(2026, 7, 1, tzinfo=timezone.utc), ledger_dir
    )

    assert (tmp_path / "ledger" / "candles_5m" / "2026-06.jsonl").exists()
    assert (tmp_path / "ledger" / "candles_5m" / "2026-07.jsonl").exists()


def test_append_swallows_write_failure(tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)
    # Must not raise.
    append_ledger_record("decisions", {"n": 1}, ledger_dir=str(tmp_path / "ledger"))
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'store.ledger'`

- [ ] **Step 3: Implement `store/ledger.py`**

```python
"""store/ledger.py -- Git-native append-only data ledger.

Every historically-meaningful fact Forge produces (market data, decisions,
closed trades, account snapshots) is appended as one JSON line per record
to a monthly-partitioned file under `ledger/`, which is committed to git
(see store/git_sync.py) instead of living only in the gitignored
data/forge.db. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.

append_ledger_record() never raises -- a ledger write must never block or
crash the caller's primary operation (heartbeat, decision cycle, trade
close), mirroring market/heartbeat.py's append_historical().
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LEDGER_DIR = "ledger"


def _partition_path(kind: str, when: datetime, ledger_dir: str) -> str:
    month = when.strftime("%Y-%m")
    return os.path.join(ledger_dir, kind, f"{month}.jsonl")


def append_ledger_record(
    kind: str,
    record: dict,
    when: datetime | None = None,
    ledger_dir: str | None = None,
) -> None:
    """Append one record as a JSON line to ledger/{kind}/{YYYY-MM}.jsonl.

    `kind` is the ledger stream name (e.g. "decisions", "candles_5m",
    "trades", "accounts"). `when` determines the month partition; defaults
    to now (UTC). `ledger_dir` defaults to the CURRENT value of module-level
    LEDGER_DIR, read at call time rather than bound into the signature at
    def time -- Python evaluates default argument values once, at function
    definition, so `ledger_dir: str = LEDGER_DIR` would silently ignore any
    later `monkeypatch.setattr(store.ledger, "LEDGER_DIR", ...)` in tests
    for every caller that relies on the default. Failure is silently
    swallowed and logged -- this path can never block or crash the caller.
    """
    try:
        moment = when or datetime.now(timezone.utc)
        effective_dir = ledger_dir if ledger_dir is not None else LEDGER_DIR
        path = _partition_path(kind, moment, effective_dir)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.warning("failed to append ledger record kind=%s", kind, exc_info=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_ledger.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add store/ledger.py tests/test_ledger.py
git commit -m "feat(ledger): add append_ledger_record, the core git-native ledger writer"
```

**Definition of done:** `append_ledger_record` writes one JSON line per call to the correct monthly partition file, creates parent directories as needed, and never raises regardless of the underlying I/O failure.

---

### Task 3: Structured wait decisions + decision-ledger wiring

**Files:**
- Modify: `agents/prompt_builder.py:193-196`
- Modify: `agents/decision_loop.py` (`log_decision` signature + its 8 call sites inside `run_decision`)
- Test: `tests/test_decision_loop.py` (extend — file already exists with `conn`/`tmp_path` fixtures, `_fresh_heartbeat_packet()`, `_config()`, `_bridge_factory()`, `AGENT_ID`/`THESIS` helpers already defined at module scope; follow those exact conventions)

**Interfaces:**
- Consumes: `store.ledger.append_ledger_record` (Task 2).
- Produces: `log_decision(conn, agent_id, action, reason, details, confidence=None, evidence_strength=None, model_used=None) -> None` — new keyword-only trailing params, backward compatible with all existing positional call sites.

- [ ] **Step 1: Ask the LLM for confidence/evidence on `wait`, not just `enter`**

In `agents/prompt_builder.py`, replace lines 193-196:

```python
You may:
  - Enter a new trade: {{"action": "enter", "asset": "...", "direction": "long|short", "entry_price": 0.0, "stop_loss_price": 0.0, "take_profit_price": 0.0, "leverage": 1, "position_size_pct": 0.10, "hypothesis": "...", "key_conditions_met": [], "key_conditions_missing": [], "confidence": 0.72, "evidence_strength": {{"funding": 0.6, "oi": 0.3, "momentum": -0.2, "volatility": 0.4}}, "uncertainty_factors": ["orderbook depth thinning reduces conviction"], "expected_value": "..."}}
  - Wait: {{"action": "wait", "reason": "..."}}
  - Close a position: {{"action": "close", "position_id": "...", "reason": "..."}}
```

with:

```python
You may:
  - Enter a new trade: {{"action": "enter", "asset": "...", "direction": "long|short", "entry_price": 0.0, "stop_loss_price": 0.0, "take_profit_price": 0.0, "leverage": 1, "position_size_pct": 0.10, "hypothesis": "...", "key_conditions_met": [], "key_conditions_missing": [], "confidence": 0.72, "evidence_strength": {{"funding": 0.6, "oi": 0.3, "momentum": -0.2, "volatility": 0.4}}, "uncertainty_factors": ["orderbook depth thinning reduces conviction"], "expected_value": "..."}}
  - Wait: {{"action": "wait", "reason": "...", "confidence": 0.35, "evidence_strength": {{"funding": 0.1, "oi": -0.2}}, "uncertainty_factors": []}}
  - Close a position: {{"action": "close", "position_id": "...", "reason": "..."}}

Wait decisions are logged and scored for calibration exactly like entries -- report your real conviction and evidence, not just a reason string. A well-calibrated 0.35 that correctly stayed out is as valuable to your track record as a well-calibrated 0.72 that entered.
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_decision_loop.py` (matching the file's existing style — see `test_decision_loop_wait_does_not_create_trade` immediately above for the exact fixture/helper conventions):

```python
@pytest.mark.asyncio
async def test_decision_loop_wait_logs_confidence_and_evidence_to_ledger(conn, tmp_path):
    """The selection-bias fix: a 'wait' decision's confidence/evidence must
    reach both the decisions table and the git-tracked ledger, not just a
    bare reason string."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def wait_llm(sys, prompt, **kwargs):
        return {
            "action": "wait",
            "reason": "days_to_event too far",
            "confidence": 0.35,
            "evidence_strength": {"unlock_size": 0.0, "days_to_event": 0.3},
        }, "Test Wait Model"

    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _fresh_heartbeat_packet())
    config = _config(heartbeat_path)

    ledger_dir = tmp_path / "ledger"
    monkeypatch_target = "store.ledger.LEDGER_DIR"
    import store.ledger as ledger_module

    original_dir = ledger_module.LEDGER_DIR
    ledger_module.LEDGER_DIR = str(ledger_dir)
    try:
        provider = MarketProvider(config)
        async with provider:
            result = await run_decision(
                agent_id=AGENT_ID,
                thesis_text=THESIS,
                config=config,
                conn=conn,
                provider=provider,
                llm_fn=wait_llm,
                bridge_factory=_bridge_factory(config),
            )
    finally:
        ledger_module.LEDGER_DIR = original_dir

    assert result["action"] == "wait"

    row = conn.execute(
        "SELECT decision_action, decision_reason FROM decisions WHERE agent_id = ?", (AGENT_ID,)
    ).fetchone()
    assert row["decision_action"] == "wait"

    from datetime import datetime, timezone
    month_file = ledger_dir / "decisions" / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
    assert month_file.exists()
    lines = month_file.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["agent"] == AGENT_ID
    assert record["action"] == "wait"
    assert record["confidence"] == 0.35
    assert record["evidence_strength"] == {"unlock_size": 0.0, "days_to_event": 0.3}
    assert record["model"] == "Test Wait Model"
```

Add `import json` at the top of `tests/test_decision_loop.py` if not already imported.

- [ ] **Step 3: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_decision_loop.py::test_decision_loop_wait_logs_confidence_and_evidence_to_ledger -v`
Expected: FAIL — no ledger file written, since `log_decision` doesn't call `append_ledger_record` yet.

- [ ] **Step 4: Implement — update `log_decision` and its call sites**

In `agents/decision_loop.py`, replace the `log_decision` function:

```python
def log_decision(
    conn,
    agent_id: str,
    action: str,
    reason: str | None,
    details: dict | None,
    confidence: float | None = None,
    evidence_strength: dict | None = None,
    model_used: str | None = None,
) -> None:
    """Log a decision to the decisions table AND the git-tracked ledger.

    confidence/evidence_strength/model_used are what the calibration goal
    depends on -- every cycle for every agent, wait included, not just
    enter. See docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
    """
    from store.db import _now
    from store.ledger import append_ledger_record

    timestamp = _now()
    conn.execute(
        """INSERT INTO decisions (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, timestamp, action, reason, json.dumps(details) if details else None),
    )
    conn.commit()

    append_ledger_record(
        "decisions",
        {
            "ts": timestamp,
            "agent": agent_id,
            "action": action,
            "reason": reason,
            "confidence": confidence,
            "evidence_strength": evidence_strength,
            "model": model_used,
        },
    )
```

Then update the call sites inside `run_decision` that have `response`/`model_label` in scope (leave the two call sites that occur *before* `model_label` exists — the heartbeat-missing branch at the top, and the "LLM returned invalid response" branch — unchanged, since neither has a response to draw from):

Line ~205 (`error` action):
```python
            log_decision(conn, agent_id, "error", reason, None, model_used=model_label)
```

Line ~211 (`wait` action — the critical fix):
```python
        if action == "wait":
            reason = response.get("reason", "")
            logger.info("[%s] LLM decided to wait: %s", agent_id, reason)
            log_decision(
                conn, agent_id, "wait", reason, None,
                confidence=response.get("confidence"),
                evidence_strength=response.get("evidence_strength"),
                model_used=model_label,
            )
            return {"action": "wait", "detail": reason}
```

Line ~225 (`close` action):
```python
            log_decision(
                conn, agent_id, "close", reason,
                {"position_id": pos_id, "fill": str(fill)},
                model_used=model_label,
            )
```

Line ~244 (`risk_blocked`):
```python
                log_decision(
                    conn, agent_id, "risk_blocked", f"risk gate blocked: {e.reason}",
                    {"risk_reason": e.reason, "order": str(response)},
                    confidence=response.get("confidence"),
                    evidence_strength=response.get("evidence_strength"),
                    model_used=model_label,
                )
```

Line ~282 (`enter`):
```python
            log_decision(
                conn, agent_id, "enter", f"entered {response['asset']}",
                {"order": str(response), "fill": str(fill)},
                confidence=response.get("confidence"),
                evidence_strength=response.get("evidence_strength"),
                model_used=model_label,
            )
```

Line ~288 (unrecognized action):
```python
        log_decision(
            conn, agent_id, "wait", f"unrecognized LLM action: {action}", None,
            model_used=model_label,
        )
```

- [ ] **Step 5: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_decision_loop.py -v`
Expected: all tests pass, including the new one.

- [ ] **Step 6: Commit**

```bash
git add agents/prompt_builder.py agents/decision_loop.py tests/test_decision_loop.py
git commit -m "feat(ledger): capture confidence/evidence on wait decisions, wire decisions into the ledger"
```

**Definition of done:** every `run_decision()` outcome (enter/wait/close/risk_blocked/error/unrecognized) writes a ledger record with whatever confidence/evidence/model data was actually available at that point in the control flow; wait decisions carry the same structured evidence shape as entries because the prompt now asks for it.

---

### Task 4: Market-data ledger export from the heartbeat; retire the old verbose mirror

**Files:**
- Modify: `market/heartbeat.py` (remove `append_historical`/`HISTORICAL_DATA_DIR`, add ledger export inside `generate_heartbeat`)
- Test: `tests/test_heartbeat_ledger.py` (new)

**Interfaces:**
- Consumes: `store.ledger.append_ledger_record` (Task 2).
- Produces: `export_heartbeat_to_ledger(packet: dict, when: datetime | None = None, ledger_dir: str | None = None) -> None` — a standalone, independently-testable function, called once at the end of `generate_heartbeat()`. Never raises; isolates failures per-asset.

- [ ] **Step 1: Check nothing else depends on the function being removed**

Run: `grep -rn "append_historical\|HISTORICAL_DATA_DIR" --include=*.py .` (or the Grep tool) from the repo root.
Expected: only `market/heartbeat.py` itself. If any other file imports these, note it and update that import to remove the dependency before continuing — do not silently leave a broken import.

- [ ] **Step 2: Write the failing test**

Create `tests/test_heartbeat_ledger.py`:

```python
from datetime import datetime, timezone

from market.heartbeat import export_heartbeat_to_ledger


def _packet():
    return {
        "timestamp": "2026-07-06T12:00:00Z",
        "assets": {
            "BTC-PERP": {
                "price": 65000.0,
                "candles_5m": [[1751803200000, 64900.0, 65100.0, 64800.0, 65000.0, 12.5]],
                "funding": 0.0001,
                "open_interest": 1000000.0,
                "liq_total_usd": 50000.0,
                "liq_long_usd": 30000.0,
                "liq_short_usd": 20000.0,
            },
        },
        "cross_asset": {},
        "regime": {"regime_tag": "range_low_vol"},
    }


def test_export_writes_one_candle_record_per_asset(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    export_heartbeat_to_ledger(_packet(), ledger_dir=ledger_dir)

    path = tmp_path / "ledger" / "candles_5m" / "2026-07.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_export_writes_funding_oi_and_liquidation_records(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    export_heartbeat_to_ledger(_packet(), ledger_dir=ledger_dir)

    for kind in ("funding", "oi", "liquidations"):
        path = tmp_path / "ledger" / kind / "2026-07.jsonl"
        assert path.exists(), f"missing {kind} ledger file"


def test_export_skips_liquidations_when_data_unavailable(tmp_path):
    packet = _packet()
    packet["assets"]["BTC-PERP"]["liq_total_usd"] = None
    ledger_dir = str(tmp_path / "ledger")
    export_heartbeat_to_ledger(packet, ledger_dir=ledger_dir)

    assert not (tmp_path / "ledger" / "liquidations" / "2026-07.jsonl").exists()
```

- [ ] **Step 3: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_heartbeat_ledger.py -v`
Expected: FAIL with `ImportError: cannot import name 'export_heartbeat_to_ledger'`

- [ ] **Step 4: Implement**

In `market/heartbeat.py`, add this import to the existing top-of-file import block (alongside `from market.regime import classify_regime`):

```python
from store.ledger import append_ledger_record
```

Then delete the entire `# Historical capture (append-only JSONL)` section (the `HISTORICAL_DATA_DIR` constant and `append_historical()` function, lines 787-815 in the current file), and replace it with:

```python
# ---------------------------------------------------------------------------
# Git-native ledger export -- replaces the old verbose full-packet mirror.
# Only the lean, backtest-relevant raw fields are exported per asset, not
# every derived indicator the heartbeat computes -- derived fields are
# recomputed from these raw inputs at read time, never trusted as frozen
# historical fact. See
# docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
# ---------------------------------------------------------------------------

def export_heartbeat_to_ledger(
    packet: dict, when: datetime | None = None, ledger_dir: str | None = None,
) -> None:
    """Decompose one heartbeat packet into lean per-type ledger records.

    `ledger_dir` defaults to None so it resolves store.ledger.LEDGER_DIR at
    call time via append_ledger_record's own None-sentinel handling --
    binding it to `= LEDGER_DIR` here would silently defeat test isolation
    the same way store/ledger.py's own docstring warns against.

    Never raises: a malformed timestamp, or one asset's malformed data,
    must not stop export for the rest of the universe or propagate into
    generate_heartbeat()'s hot path -- each asset is isolated so a single
    bad entry degrades only that asset's records, not the whole cycle.
    """
    try:
        ts = packet.get("timestamp")
        if not ts:
            return
        moment = when or datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        logger.warning(
            "export_heartbeat_to_ledger: bad timestamp %r", packet.get("timestamp"),
            exc_info=True,
        )
        return

    for asset, fields in (packet.get("assets") or {}).items():
        try:
            candle = (fields.get("candles_5m") or [None])[-1]
            if candle is not None:
                append_ledger_record(
                    "candles_5m",
                    {"ts": ts, "asset": asset, "o": candle[1], "h": candle[2],
                     "l": candle[3], "c": candle[4], "v": candle[5]},
                    moment, ledger_dir,
                )

            if fields.get("funding") is not None:
                append_ledger_record(
                    "funding", {"ts": ts, "asset": asset, "rate": fields["funding"]},
                    moment, ledger_dir,
                )

            if fields.get("open_interest") is not None:
                append_ledger_record(
                    "oi", {"ts": ts, "asset": asset, "oi": fields["open_interest"]},
                    moment, ledger_dir,
                )

            liq_total = fields.get("liq_total_usd")
            if liq_total is not None:
                append_ledger_record(
                    "liquidations",
                    {
                        "ts": ts, "asset": asset, "total_usd": liq_total,
                        "long_usd": fields.get("liq_long_usd"),
                        "short_usd": fields.get("liq_short_usd"),
                    },
                    moment, ledger_dir,
                )
        except Exception:
            logger.warning(
                "export_heartbeat_to_ledger: failed for asset %s", asset, exc_info=True,
            )
```

Then replace the single line `append_historical(packet)` (in `generate_heartbeat()`, right before `return packet`) with:

```python
    export_heartbeat_to_ledger(packet)
```

- [ ] **Step 5: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_heartbeat_ledger.py -v`
Expected: 3 passed

Then run the full heartbeat test suite to confirm nothing that depended on `append_historical` broke:
Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_heartbeat.py -v` (adjust filename if the existing heartbeat test file is named differently -- confirm via `ls tests/test_heartbeat*` first)
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add market/heartbeat.py tests/test_heartbeat_ledger.py
git commit -m "feat(ledger): export lean market data to the ledger, retire the verbose heartbeat JSONL mirror"
```

**Definition of done:** every heartbeat cycle appends one ledger record per asset per data type (candles_5m, funding, oi, and liquidations when available) instead of one giant verbose JSON blob to `data/historical_data/`; `append_historical()` and `HISTORICAL_DATA_DIR` no longer exist anywhere in the codebase.

---

### Task 5: Trade-close and account-snapshot ledger export

**Files:**
- Modify: `store/positions.py` (`execute_close`)
- Test: `tests/test_positions_ledger.py` (new)

**Interfaces:**
- Consumes: `store.ledger.append_ledger_record` (Task 2).
- Produces: no new public function — `execute_close`'s existing signature and return value are unchanged; this task only adds side effects.

- [ ] **Step 1: Write the failing test**

Create `tests/test_positions_ledger.py`:

```python
import json
import sqlite3
from pathlib import Path

import pytest

from store.db import init_schema, insert_account_snapshot, insert_agent, insert_position, insert_trade
from store.positions import execute_close


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    init_schema(c)
    yield c
    c.close()


def _seed_open_trade(conn):
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "sage_turtle", "paper", 50000.0, 50000.0)
    trade = {
        "id": "sage_turtle_20260706_120000_FET",
        "agent_id": "sage_turtle",
        "mode": "paper",
        "asset": "FET-PERP",
        "direction": "short",
        "entry_price": 1.50,
        "stop_loss_price": 1.545,
        "take_profit_price": 1.41,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-07-06T12:00:00Z",
        "status": "open",
        "ohlcv_15m_40_blob": b"\x81\xa4test",
    }
    insert_trade(conn, trade)
    position = dict(trade)
    position["id"] = "pos_" + trade["id"]
    position["trade_id"] = trade["id"]
    position["opened_at"] = trade["entry_timestamp"]
    insert_position(conn, position)
    return position


def test_execute_close_writes_full_trade_record_to_ledger(conn, tmp_path, monkeypatch):
    import store.ledger as ledger_module

    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))
    position = _seed_open_trade(conn)

    execute_close(
        conn=conn, position_id=position["id"], exit_price=1.44, reason="take_profit",
        config={"taker_fee": 0.00035}, position_dict=position, funding_history=[],
    )

    from datetime import datetime, timezone
    month = f"{datetime.now(timezone.utc):%Y-%m}"
    trades_path = tmp_path / "ledger" / "trades" / f"{month}.jsonl"
    assert trades_path.exists()
    record = json.loads(trades_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["id"] == position["trade_id"]
    assert record["status"] == "closed"
    assert record["exit_price"] == 1.44
    assert "ohlcv_15m_40_blob" not in record  # excluded: redundant with the market-data ledger, and bytes don't round-trip through JSON


def test_execute_close_writes_account_snapshot_to_ledger(conn, tmp_path, monkeypatch):
    import store.ledger as ledger_module

    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))
    position = _seed_open_trade(conn)

    execute_close(
        conn=conn, position_id=position["id"], exit_price=1.44, reason="take_profit",
        config={"taker_fee": 0.00035}, position_dict=position, funding_history=[],
    )

    from datetime import datetime, timezone
    month = f"{datetime.now(timezone.utc):%Y-%m}"
    accounts_path = tmp_path / "ledger" / "accounts" / f"{month}.jsonl"
    assert accounts_path.exists()
    record = json.loads(accounts_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["agent_id"] == "sage_turtle"
    assert record["mode"] == "paper"
    assert record["balance"] > 0
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_positions_ledger.py -v`
Expected: FAIL — no ledger directory created, since `execute_close` doesn't export yet.

- [ ] **Step 3: Implement**

In `store/positions.py`, add near the top (after the existing imports):

```python
_TRADE_LEDGER_EXCLUDE_COLUMNS = {
    # Redundant with the candles_5m/funding ledger (Task 4) once a trade's
    # timestamp is known, and raw `bytes` blobs don't round-trip through
    # JSON -- exporting them would either crash json.dumps or silently
    # write an unrestorable str(bytes) repr.
    "ohlcv_15m_40_blob", "ohlcv_1h_20_blob", "ohlcv_4h_10_blob", "funding_history_blob",
}
```

Then, in `execute_close`, immediately after the `with conn:` block that does the UPDATE/DELETE/INSERT (i.e. right after the block closes, before `return {...}`), add:

```python
    from store.ledger import append_ledger_record

    full_trade = conn.execute(
        "SELECT * FROM trades WHERE id = ?", (position_dict["trade_id"],)
    ).fetchone()
    if full_trade:
        record = {
            k: v for k, v in dict(full_trade).items()
            if k not in _TRADE_LEDGER_EXCLUDE_COLUMNS
        }
        append_ledger_record("trades", record)

    append_ledger_record(
        "accounts",
        {
            "ts": now,
            "agent_id": position_dict["agent_id"],
            "mode": position_dict.get("mode", "paper"),
            "balance": new_balance,
            "peak_balance": peak,
        },
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_positions_ledger.py -v`
Expected: 2 passed

Then run the existing positions test suite to confirm no regression: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_positions.py -v` (confirm the exact existing filename via `ls tests/test_positions*` first).

- [ ] **Step 5: Commit**

```bash
git add store/positions.py tests/test_positions_ledger.py
git commit -m "feat(ledger): export closed trades and account snapshots to the ledger on every close"
```

**Definition of done:** every call to `execute_close` (from both paper and live bridges, and from `reconcile_positions`' auto SL/TP hits) appends one self-contained closed-trade record (entry + exit fields together, blobs excluded) and one account-balance record to the ledger, in addition to the existing SQLite writes.

---

### Task 6: Current-state snapshot writer

**Files:**
- Create: `store/state_snapshot.py`
- Modify: `forge.py` (`run_heartbeat_cycle`)
- Test: `tests/test_state_snapshot.py` (new)

**Interfaces:**
- Consumes: `store.db.get_latest_account`, `store.positions.get_all_open_positions` (both already exist, verified above).
- Produces: `write_current_state(conn, path: str = DEFAULT_STATE_PATH) -> None`, called from `forge.py`'s `run_heartbeat_cycle`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_state_snapshot.py`:

```python
import json
import sqlite3

import pytest

from store.db import init_schema, insert_account_snapshot, insert_agent, insert_position, insert_trade
from store.state_snapshot import write_current_state


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    init_schema(c)
    yield c
    c.close()


def test_write_current_state_captures_agents_and_balances(conn, tmp_path):
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "sage_turtle", "paper", 51200.0, 52000.0)

    path = str(tmp_path / "state" / "current.json")
    write_current_state(conn, path)

    state = json.loads((tmp_path / "state" / "current.json").read_text(encoding="utf-8"))
    assert state["agents"][0]["id"] == "sage_turtle"
    assert state["agents"][0]["paper_balance"] == 51200.0


def test_write_current_state_captures_open_positions(conn, tmp_path):
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "sage_turtle", "paper", 50000.0, 50000.0)
    trade = {
        "id": "t1", "agent_id": "sage_turtle", "mode": "paper", "asset": "FET-PERP",
        "direction": "short", "entry_price": 1.5, "status": "open",
    }
    insert_trade(conn, trade)
    insert_position(conn, {
        "id": "pos_t1", "agent_id": "sage_turtle", "asset": "FET-PERP", "direction": "short",
        "entry_price": 1.5, "stop_loss_price": 1.545, "take_profit_price": 1.41,
        "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
        "opened_at": "2026-07-06T12:00:00Z", "mode": "paper", "trade_id": "t1",
    })

    path = str(tmp_path / "state" / "current.json")
    write_current_state(conn, path)

    state = json.loads((tmp_path / "state" / "current.json").read_text(encoding="utf-8"))
    assert len(state["open_positions"]) == 1
    assert state["open_positions"][0]["id"] == "pos_t1"


def test_write_current_state_is_atomic(conn, tmp_path):
    """No .tmp file left behind after a successful write."""
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    path = str(tmp_path / "state" / "current.json")
    write_current_state(conn, path)

    assert not (tmp_path / "state" / "current.json.tmp").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_state_snapshot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'store.state_snapshot'`

- [ ] **Step 3: Implement**

Create `store/state_snapshot.py`:

```python
"""store/state_snapshot.py -- git-tracked current-state snapshot.

Unlike ledger/ (append-only history), state/current.json is overwritten
every cycle: it captures *right now* -- open positions, live balances,
agent status -- so a fresh `git clone` restores exactly where the desk
left off, not just its history. Small and bounded by agent count, so
committing it every cycle (see store/git_sync.py) is cheap. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = os.path.join("state", "current.json")


def build_current_state(conn) -> dict:
    from store.db import get_latest_account
    from store.positions import get_all_open_positions

    agents = [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, status, current_thesis_version, last_model_used FROM agents"
        ).fetchall()
    ]
    for agent in agents:
        paper = get_latest_account(conn, agent["id"], "paper")
        agent["paper_balance"] = paper["balance"] if paper else None
        agent["paper_peak"] = paper["peak_balance"] if paper else None
        live = get_latest_account(conn, agent["id"], "live")
        agent["live_balance"] = live["balance"] if live else None

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": agents,
        "open_positions": get_all_open_positions(conn),
    }


def write_current_state(conn, path: str = DEFAULT_STATE_PATH) -> None:
    """Atomically overwrite `path` with the current desk state. Best-effort
    -- must never block or crash the heartbeat cycle that calls it."""
    try:
        state = build_current_state(conn)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        logger.warning("failed to write current-state snapshot", exc_info=True)
```

Then in `forge.py`, add `from store.state_snapshot import write_current_state` to the imports, and add a call at the end of `run_heartbeat_cycle` (after the existing `finally: conn.close()` block's position -- i.e. call it using its own connection, mirroring how the existing function already opens/closes its own `conn` for `reconcile_positions`/`update_position_pnl`):

```python
async def run_heartbeat_cycle(provider, config: dict) -> None:
    """One heartbeat generation cycle -- wrapped for both the immediate
    startup run and the recurring APScheduler job."""
    packet = await heartbeat.generate_heartbeat(provider, config)
    logger.info(
        "Heartbeat cycle complete: %d assets written at %s",
        len(packet.get("assets", {})),
        packet.get("timestamp"),
    )
    assets_data = packet.get("assets", {})
    if assets_data:
        conn = get_connection(str(DB_PATH))
        try:
            closed = await reconcile_positions(conn, assets_data, provider, config)
            if closed:
                logger.info("SL/TP reconciled %d position(s)", closed)
            update_position_pnl(conn, assets_data)
            write_current_state(conn)
        except Exception:
            logger.warning(
                "Failed to update position PnL from heartbeat", exc_info=True
            )
        finally:
            conn.close()
```

(`write_current_state` is already internally exception-safe, but leaving it inside the existing `try` is harmless and keeps the diff minimal.)

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_state_snapshot.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add store/state_snapshot.py forge.py tests/test_state_snapshot.py
git commit -m "feat(ledger): write state/current.json every heartbeat cycle"
```

**Definition of done:** after any heartbeat cycle, `state/current.json` reflects the exact current agent roster, balances, and open positions — not history, just now — written atomically so it's never observed half-written.

---

### Task 7: Git sync — commit + push every cycle, non-blocking

**Files:**
- Create: `store/git_sync.py`
- Modify: `forge.py` (add scheduler job)
- Test: `tests/test_git_sync.py` (new)

**Interfaces:**
- Produces: `sync_to_git(repo_root: Path, paths: tuple[str, ...] = TRACKED_PATHS) -> bool` — returns whether a commit was made.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_git_sync.py`:

```python
import subprocess
from pathlib import Path

from store.git_sync import sync_to_git


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


def test_sync_commits_new_ledger_file(tmp_path):
    _init_repo(tmp_path)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "decisions.jsonl").write_text('{"a": 1}\n')

    committed = sync_to_git(tmp_path)

    assert committed is True
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "chore(ledger)" in log.stdout


def test_sync_no_changes_returns_false(tmp_path):
    _init_repo(tmp_path)
    committed = sync_to_git(tmp_path)
    assert committed is False


def test_sync_swallows_failure_on_non_git_directory(tmp_path):
    # No git repo initialized at all -> `git add` fails; must not raise.
    (tmp_path / "ledger").mkdir()
    committed = sync_to_git(tmp_path)
    assert committed is False
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_git_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'store.git_sync'`

- [ ] **Step 3: Implement**

Create `store/git_sync.py`:

```python
"""store/git_sync.py -- best-effort commit + push of the git-native ledger.

Runs on the same cadence as the heartbeat. Never raises, never blocks the
caller -- a failed or slow git operation must not stall the trading loop,
same defensive contract as market/heartbeat.py's append_historical(). See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 30
TRACKED_PATHS: tuple[str, ...] = ("ledger", "state")


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result


def sync_to_git(repo_root: Path, paths: tuple[str, ...] = TRACKED_PATHS) -> bool:
    """Stage and commit `paths`, then attempt to push.

    Returns True if a commit was made -- regardless of whether the push
    succeeded, since a failed push just retries next cycle: the *next*
    commit's push carries every prior unpushed commit along with it, so no
    data is lost by a transient network failure here.

    `paths` is filtered to entries that exist on disk before staging --
    real git treats `git add` with ANY nonexistent pathspec as a fatal
    error for the whole invocation, even when other pathspecs match. This
    matters in production, not just in tests: on a fresh clone, neither
    ledger/ nor state/ exists until the first heartbeat/decision cycle
    writes to it, so an unfiltered `git add ledger state` would fail
    outright on the very first sync attempt.
    """
    committed = False
    existing = [p for p in paths if (repo_root / p).exists()]
    if not existing:
        return False
    try:
        _run(["git", "add", *existing], repo_root)
        staged = _run(["git", "diff", "--cached", "--quiet"], repo_root, check=False)
        if staged.returncode != 0:
            _run(["git", "commit", "-m", "chore(ledger): heartbeat sync"], repo_root)
            committed = True
    except Exception:
        logger.warning("git ledger commit failed", exc_info=True)
        return committed

    try:
        _run(["git", "push"], repo_root)
    except Exception:
        logger.warning("git ledger push failed (will retry next cycle)", exc_info=True)

    return committed
```

Then in `forge.py`, add `from pathlib import Path` (already imported), `from store.git_sync import sync_to_git`, and register a new scheduler job in `main()` right after the existing `"counterfactual"` job registration:

```python
    # ------------------------------------------------------------------
    # Ledger git sync -- commits + pushes ledger/ and state/ every cycle.
    # Best-effort: a failed push just retries next cycle (see
    # store/git_sync.py). Runs on the heartbeat cadence so it never lags
    # more than one cycle behind what agents actually wrote.
    # ------------------------------------------------------------------
    async def _run_git_sync_job():
        try:
            committed = await asyncio.get_event_loop().run_in_executor(
                None, sync_to_git, Path(__file__).resolve().parent
            )
            if committed:
                logger.info("Ledger git sync: committed and pushed")
        except Exception:
            logger.warning("Ledger git sync job failed", exc_info=True)

    scheduler.add_job(
        _run_git_sync_job,
        trigger=IntervalTrigger(seconds=heartbeat_interval, timezone=timezone.utc),
        id="ledger_git_sync",
        replace_existing=True,
    )
    logger.info("Ledger git sync scheduler started -- runs every %ds", heartbeat_interval)
```

(`run_in_executor` because `sync_to_git` calls blocking `subprocess.run` -- must not block the asyncio event loop that's also running the web server and the heartbeat job.)

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_git_sync.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add store/git_sync.py forge.py tests/test_git_sync.py
git commit -m "feat(ledger): commit and push ledger/state every heartbeat cycle, non-blocking"
```

**Definition of done:** every heartbeat cycle, any new/changed files under `ledger/` or `state/` get committed and pushed automatically; a git or network failure is logged and retried next cycle, never raised into the trading loop.

---

### Task 8: Monthly compaction (JSONL → Parquet + resolution decay)

**Files:**
- Create: `scripts/compact_ledger.py`
- Test: `tests/test_compact_ledger.py` (new)

**Interfaces:**
- Produces: `compact_ledger(ledger_dir: Path, decay_window_months: int = 12) -> list[Path]`, `compact_file(path: Path, decay_window_months: int) -> Path`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compact_ledger.py`:

```python
import json
from pathlib import Path

import pandas as pd

from scripts.compact_ledger import _closed_month_files, _current_month, compact_ledger


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_closed_month_excludes_current_month(tmp_path):
    current = _current_month()
    _write_jsonl(tmp_path / "decisions" / f"{current}.jsonl", [{"n": 1}])
    _write_jsonl(tmp_path / "decisions" / "2020-01.jsonl", [{"n": 2}])

    closed = _closed_month_files(tmp_path)

    assert [p.name for p in closed] == ["2020-01.jsonl"]


def test_compact_converts_jsonl_to_parquet_and_deletes_source(tmp_path):
    _write_jsonl(
        tmp_path / "decisions" / "2020-01.jsonl",
        [{"ts": "2020-01-01T00:00:00Z", "agent": "sage_turtle", "action": "wait"}],
    )

    written = compact_ledger(tmp_path)

    assert len(written) == 1
    assert written[0].name == "2020-01.parquet"
    assert not (tmp_path / "decisions" / "2020-01.jsonl").exists()
    df = pd.read_parquet(written[0])
    assert df.iloc[0]["agent"] == "sage_turtle"


def test_compact_downsamples_old_candles_to_hourly(tmp_path):
    records = [
        {"ts": f"2020-01-01T00:{m:02d}:00Z", "asset": "BTC-PERP", "c": float(m)}
        for m in (0, 5, 10, 15)
    ]
    _write_jsonl(tmp_path / "candles_5m" / "2020-01.jsonl", records)

    written = compact_ledger(tmp_path, decay_window_months=1)

    df = pd.read_parquet(written[0])
    assert len(df) == 1  # all four 5m samples collapse into one hourly bucket


def test_compact_does_not_downsample_recent_candles(tmp_path):
    current = _current_month()
    # Force a "closed but recent" month by writing last month's data -- if
    # the test runs near a month boundary this could be flaky at the
    # granularity of "current vs not", but decay_window_months=12 makes it
    # safe regardless of which specific closed month it lands on.
    records = [
        {"ts": "2020-01-01T00:00:00Z", "asset": "BTC-PERP", "c": 1.0},
        {"ts": "2020-01-01T00:05:00Z", "asset": "BTC-PERP", "c": 2.0},
    ]
    _write_jsonl(tmp_path / "candles_5m" / "2020-01.jsonl", records)

    written = compact_ledger(tmp_path, decay_window_months=1200)  # effectively "never decay"

    df = pd.read_parquet(written[0])
    assert len(df) == 2  # no downsampling -- still within the decay window
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_compact_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.compact_ledger'`

- [ ] **Step 3: Implement**

Create `scripts/compact_ledger.py`:

```python
#!/usr/bin/env python
"""scripts/compact_ledger.py -- Monthly ledger compaction.

Converts closed-month JSONL ledger partitions to Parquet (smaller,
columnar) and, for the highest-volume fine-grained streams, downsamples
data older than DECAY_WINDOW_MONTHS to hourly resolution. Never touches
the current month's hot JSONL file -- only fully-closed months are
eligible. Idempotent: re-running against an already-compacted month is a
no-op since the source .jsonl no longer exists. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

LEDGER_DIR = Path(__file__).resolve().parent.parent / "ledger"
DECAY_WINDOW_MONTHS = 12
DECAY_ELIGIBLE_KINDS = {"candles_5m", "oi"}


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _closed_month_files(ledger_dir: Path) -> list[Path]:
    current = _current_month()
    return sorted(p for p in ledger_dir.glob("*/*.jsonl") if p.stem < current)


def _months_ago(month_str: str) -> int:
    year, month = (int(x) for x in month_str.split("-"))
    now = datetime.now(timezone.utc)
    return (now.year - year) * 12 + (now.month - month)


def compact_file(path: Path, decay_window_months: int = DECAY_WINDOW_MONTHS) -> Path:
    """Convert one closed-month .jsonl file to .parquet, deleting the
    source. If the file's kind is decay-eligible and its month is older
    than `decay_window_months`, downsample to hourly (first sample of each
    UTC hour per asset) before writing."""
    kind = path.parent.name
    month = path.stem

    df = pd.read_json(path, lines=True)
    if kind in DECAY_ELIGIBLE_KINDS and _months_ago(month) > decay_window_months and not df.empty:
        ts = pd.to_datetime(df["ts"], utc=True)
        df = (
            df.assign(_hour=ts.dt.floor("h"))
            .sort_values("ts")
            .groupby(["_hour", "asset"], as_index=False)
            .first()
            .drop(columns=["_hour"])
        )

    out_path = path.with_suffix(".parquet")
    df.to_parquet(out_path, engine="pyarrow", index=False)
    path.unlink()
    return out_path


def compact_ledger(
    ledger_dir: Path = LEDGER_DIR, decay_window_months: int = DECAY_WINDOW_MONTHS
) -> list[Path]:
    written = []
    for path in _closed_month_files(ledger_dir):
        out = compact_file(path, decay_window_months)
        written.append(out)
        print(f"Compacted {path} -> {out}")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Compact closed-month ledger JSONL to Parquet")
    parser.add_argument("--ledger-dir", type=Path, default=LEDGER_DIR)
    parser.add_argument("--decay-window-months", type=int, default=DECAY_WINDOW_MONTHS)
    args = parser.parse_args()
    compact_ledger(args.ledger_dir, args.decay_window_months)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_compact_ledger.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/compact_ledger.py tests/test_compact_ledger.py
git commit -m "feat(ledger): add monthly JSONL-to-Parquet compaction with resolution decay"
```

**Definition of done:** running `python scripts/compact_ledger.py` converts every closed month's `.jsonl` to `.parquet` and deletes the source; `candles_5m`/`oi` months older than 12 months are additionally downsampled to hourly; the current month is never touched; re-running is a safe no-op.

---

### Task 9: Rebuild-local-cache — the disaster-recovery proof

**Files:**
- Create: `scripts/rebuild_local_cache.py`
- Test: `tests/test_rebuild_local_cache.py` (new)

**Interfaces:**
- Consumes: `store.db.get_connection`, `store.db.init_schema`, `store.db.insert_agent`, `store.db.insert_trade`, `store.db.insert_account_snapshot`, `store.db.insert_position` (all verified to exist above).
- Produces: `rebuild(db_path: Path, ledger_dir: Path, state_path: Path) -> dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rebuild_local_cache.py`:

```python
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.rebuild_local_cache import rebuild


def _write_state(path: Path, agents: list[dict], open_positions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": "2026-07-06T12:00:00Z", "agents": agents, "open_positions": open_positions}),
        encoding="utf-8",
    )


def _write_ledger_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_rebuild_refuses_to_overwrite_existing_db(tmp_path):
    db_path = tmp_path / "forge.db"
    db_path.write_text("not empty")
    state_path = tmp_path / "state" / "current.json"
    _write_state(state_path, [], [])

    with pytest.raises(FileExistsError):
        rebuild(db_path, tmp_path / "ledger", state_path)


def test_rebuild_seeds_agents_and_balances_from_state(tmp_path):
    db_path = tmp_path / "forge.db"
    state_path = tmp_path / "state" / "current.json"
    _write_state(
        state_path,
        agents=[{
            "id": "sage_turtle", "name": "sage_turtle", "status": "active",
            "current_thesis_version": 2, "last_model_used": "qwen3.6-35b",
            "paper_balance": 51200.0, "paper_peak": 52000.0, "live_balance": None,
        }],
        open_positions=[],
    )

    summary = rebuild(db_path, tmp_path / "ledger", state_path)

    assert summary["agents"] == 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agents WHERE id = 'sage_turtle'").fetchone()
    assert row["status"] == "active"
    assert row["current_thesis_version"] == 2
    conn.close()


def test_rebuild_replays_trades_and_accounts_from_ledger(tmp_path):
    db_path = tmp_path / "forge.db"
    state_path = tmp_path / "state" / "current.json"
    _write_state(
        state_path,
        agents=[{
            "id": "sage_turtle", "name": "sage_turtle", "status": "active",
            "current_thesis_version": 1, "last_model_used": None,
            "paper_balance": 50500.0, "paper_peak": 50500.0, "live_balance": None,
        }],
        open_positions=[],
    )
    _write_ledger_jsonl(
        tmp_path / "ledger" / "trades" / "2026-07.jsonl",
        [{
            "id": "t1", "agent_id": "sage_turtle", "mode": "paper", "asset": "FET-PERP",
            "direction": "short", "entry_price": 1.5, "exit_price": 1.44,
            "status": "closed", "result": "win",
        }],
    )
    _write_ledger_jsonl(
        tmp_path / "ledger" / "accounts" / "2026-07.jsonl",
        [{"agent_id": "sage_turtle", "mode": "paper", "balance": 50500.0, "peak_balance": 50500.0}],
    )

    summary = rebuild(db_path, tmp_path / "ledger", state_path)

    assert summary["trades"] == 1
    assert summary["accounts"] == 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM trades WHERE id = 't1'").fetchone()
    assert trade["status"] == "closed"
    assert trade["exit_price"] == 1.44
    conn.close()


def test_rebuild_reopens_positions_from_state(tmp_path):
    db_path = tmp_path / "forge.db"
    state_path = tmp_path / "state" / "current.json"
    open_position = {
        "id": "pos_t2", "agent_id": "sage_turtle", "asset": "TIA-PERP", "direction": "long",
        "entry_price": 4.2, "stop_loss_price": 4.0, "take_profit_price": 4.6,
        "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
        "opened_at": "2026-07-06T11:00:00Z", "mode": "paper", "trade_id": "t2",
    }
    _write_state(
        state_path,
        agents=[{
            "id": "sage_turtle", "name": "sage_turtle", "status": "active",
            "current_thesis_version": 1, "last_model_used": None,
            "paper_balance": 50000.0, "paper_peak": 50000.0, "live_balance": None,
        }],
        open_positions=[open_position],
    )
    _write_ledger_jsonl(
        tmp_path / "ledger" / "trades" / "2026-07.jsonl",
        [{
            "id": "t2", "agent_id": "sage_turtle", "mode": "paper", "asset": "TIA-PERP",
            "direction": "long", "entry_price": 4.2, "status": "open",
        }],
    )

    summary = rebuild(db_path, tmp_path / "ledger", state_path)

    assert summary["open_positions_in_state"] == 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    pos = conn.execute("SELECT * FROM positions WHERE id = 'pos_t2'").fetchone()
    assert pos is not None
    assert pos["asset"] == "TIA-PERP"
    conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_rebuild_local_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.rebuild_local_cache'`

- [ ] **Step 3: Implement**

Create `scripts/rebuild_local_cache.py`:

```python
#!/usr/bin/env python
"""scripts/rebuild_local_cache.py -- Disaster-recovery rebuild of data/forge.db.

Reconstructs the local, gitignored SQLite cache purely from the git-tracked
ledger/ and state/ directories. This is the concrete proof of the "burned
laptop -> git pull -> back to normal" requirement: after cloning the repo
fresh, run this once before `python forge.py`. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from store.db import (
    get_connection,
    init_schema,
    insert_account_snapshot,
    insert_agent,
    insert_position,
    insert_trade,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "forge.db"
DEFAULT_LEDGER_DIR = PROJECT_ROOT / "ledger"
DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "current.json"


def _read_partitions(ledger_dir: Path, kind: str) -> pd.DataFrame:
    """Concatenate every .parquet and .jsonl partition for one ledger kind,
    oldest to newest. Empty DataFrame if the kind has no data yet."""
    kind_dir = ledger_dir / kind
    if not kind_dir.exists():
        return pd.DataFrame()
    frames = [pd.read_parquet(p) for p in sorted(kind_dir.glob("*.parquet"))]
    frames += [pd.read_json(p, lines=True) for p in sorted(kind_dir.glob("*.jsonl"))]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def rebuild(
    db_path: Path = DEFAULT_DB_PATH,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    state_path: Path = DEFAULT_STATE_PATH,
) -> dict:
    """Rebuild db_path from scratch using only the git-tracked ledger and
    state snapshot. Refuses to run against an existing db_path -- move it
    aside first if you really want to rebuild over it."""
    if db_path.exists():
        raise FileExistsError(
            f"{db_path} already exists -- refusing to overwrite. "
            "Move it aside first if you really want to rebuild."
        )

    state = json.loads(state_path.read_text(encoding="utf-8"))

    conn = get_connection(str(db_path))
    init_schema(conn)

    for agent in state["agents"]:
        insert_agent(conn, agent["id"], agent["name"], state["generated_at"], "{}")
        conn.execute(
            "UPDATE agents SET status = ?, current_thesis_version = ?, last_model_used = ? WHERE id = ?",
            (agent["status"], agent["current_thesis_version"], agent["last_model_used"], agent["id"]),
        )
    conn.commit()

    trades_df = _read_partitions(ledger_dir, "trades")
    for _, row in trades_df.iterrows():
        try:
            insert_trade(conn, row.dropna().to_dict())
        except Exception as exc:
            raise RuntimeError(
                f"Failed to replay trade {row.get('id')!r} for agent "
                f"{row.get('agent_id')!r} -- is this agent missing from "
                f"state/current.json's agents list? ({exc})"
            ) from exc

    accounts_df = _read_partitions(ledger_dir, "accounts")
    for _, row in accounts_df.iterrows():
        try:
            insert_account_snapshot(conn, row["agent_id"], row["mode"], row["balance"], row["peak_balance"])
        except Exception as exc:
            raise RuntimeError(
                f"Failed to replay account snapshot for agent "
                f"{row.get('agent_id')!r} mode {row.get('mode')!r} -- is this "
                f"agent missing from state/current.json's agents list? ({exc})"
            ) from exc

    # state's own paper_balance/paper_peak is authoritative for "right now"
    # (that's the whole point of a snapshot committed every cycle, separate
    # from the append-only ledger) -- insert it LAST so it becomes the
    # latest account row regardless of whether the ledger's accounts
    # stream was complete. Without this, a gap in the ledger accounts
    # replay above would silently leave the rebuilt DB with a stale or
    # missing balance even though state/current.json recorded the truth.
    for agent in state["agents"]:
        paper_balance = agent.get("paper_balance")
        paper_peak = agent.get("paper_peak")
        if paper_balance is not None:
            insert_account_snapshot(
                conn, agent["id"], "paper", paper_balance,
                paper_peak if paper_peak is not None else paper_balance,
            )
        else:
            insert_account_snapshot(conn, agent["id"], "paper", 50000.0, 50000.0)

    for position in state.get("open_positions", []):
        try:
            trade_id = position.get("trade_id")
            if trade_id is not None:
                # No closed-trade ledger record exists for a still-open
                # position (execute_close only ever ledger-exports on
                # CLOSE) -- synthesize a minimal "open" trades row from
                # the position snapshot so insert_position's FK
                # (positions.trade_id -> trades.id) is satisfiable.
                # insert_trade's INSERT OR IGNORE makes this a no-op if a
                # real (closed) record for this id was already replayed
                # from the ledger above.
                insert_trade(conn, {
                    "id": trade_id,
                    "agent_id": position["agent_id"],
                    "mode": position.get("mode", "paper"),
                    "asset": position["asset"],
                    "direction": position["direction"],
                    "entry_price": position.get("entry_price"),
                    "stop_loss_price": position.get("stop_loss_price"),
                    "take_profit_price": position.get("take_profit_price"),
                    "leverage": position.get("leverage"),
                    "position_size_pct": position.get("position_size_pct"),
                    "notional_usd": position.get("notional_usd"),
                    "entry_timestamp": position.get("opened_at"),
                    "status": "open",
                })
            insert_position(conn, position)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to reopen position {position.get('id')!r} for agent "
                f"{position.get('agent_id')!r} -- is this agent missing from "
                f"state/current.json's agents list? ({exc})"
            ) from exc

    conn.close()

    return {
        "db_path": str(db_path),
        "agents": len(state["agents"]),
        "trades": len(trades_df),
        "accounts": len(accounts_df),
        "open_positions_in_state": len(state.get("open_positions", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild data/forge.db from the git ledger")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--ledger-dir", type=Path, default=DEFAULT_LEDGER_DIR)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args()

    try:
        summary = rebuild(args.db_path, args.ledger_dir, args.state_path)
    except Exception as exc:
        print(f"rebuild failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(
        f"Rebuilt {summary['db_path']}: {summary['agents']} agent(s), "
        f"{summary['trades']} trade(s), {summary['accounts']} account snapshot(s), "
        f"{summary['open_positions_in_state']} open position(s) restored."
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/test_rebuild_local_cache.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/rebuild_local_cache.py tests/test_rebuild_local_cache.py
git commit -m "feat(ledger): add rebuild_local_cache.py, the disaster-recovery proof"
```

**Definition of done:** on a machine with only the git-tracked `ledger/` and `state/current.json` present (no `data/forge.db`), `python scripts/rebuild_local_cache.py` produces a `data/forge.db` with the same agents, balances, closed trades, and open positions as the machine that was lost — this is the literal "burned laptop" test.

---

### Task 10: `.gitignore` cleanup — retire the superseded path

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Remove the now-dead `data/historical_data/` rule and dedupe**

Replace the full contents of `.gitignore` with:

```
.env
*.pyc
__pycache__/
*.db
*.db-wal
*.db-shm

data/backups/
data/keystores/
data/forge.log
data/heartbeat.json
data/heartbeat_oi_history.json
.opencode/status
logs/
.pytest_cache/
*.egg-info/
dist/
build/
.venv/
venv/
nul
```

(Removes the now-superseded `data/historical_data/` line and the three duplicated entries; does not add `ledger/` or `state/` — they must NOT be ignored, since being trackable by default is the entire point.)

- [ ] **Step 2: Verify new ledger paths are actually trackable**

Run: `mkdir -p ledger/decisions && echo '{}' > ledger/decisions/2026-07.jsonl && git status --short`
Expected: `ledger/decisions/2026-07.jsonl` shows as an untracked (`??`) file, NOT silently absent from `git status` output.

Then clean up the manual test file: `rm -rf ledger/decisions/2026-07.jsonl` (leave it if Task 1-9 have already populated real ledger content by this point in execution order — this step is just a gitignore sanity check, not meant to leave stray files if run in isolation).

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore(ledger): retire data/historical_data/ gitignore rule, dedupe entries"
```

**Definition of done:** `git status` shows new files under `ledger/` and `state/` as trackable; `data/forge.db` and friends remain ignored.

---

## Known gap: reflections / evaluations / thesis versions

The design spec's data inventory (§2) includes `reflections`, `evaluations`, and thesis-version records. This plan does not add ledger export for them: no code currently writes to those tables at runtime (the M8 reflection pipeline that would populate them doesn't exist yet — STRATEGIC_ASSESSMENT_2026-07-04.md confirms 0 reflections, 0 evaluations as of the last audit, and agents are only ever seeded with a v1 thesis today, never revised). Adding ledger export for empty tables would be untested, unexercised code. When the M8 reflection pipeline is built, its spec/plan should add `append_ledger_record("reflections", ...)` / `("evaluations", ...)` / `("theses", ...)` calls at the point each row is actually written, following the exact pattern established in Tasks 3 and 5 here.

## Execution notes

Tasks 2 through 9 have no cross-dependencies on each other's *internals* beyond Task 2 (`store/ledger.py`), which everything else imports — so Task 2 must land first, then Tasks 3-7 can proceed in any order (they touch disjoint files), and Tasks 8-9 depend on the record shapes Tasks 3-5 establish. Task 1 (reset) and Task 10 (gitignore) are independent of all the others and can happen anytime, but doing Task 1 last (immediately before first real use) avoids wiping data generated while testing Tasks 2-9 locally.

After all tasks: run the full suite once —
`C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v --ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py`
— before considering the plan complete.
