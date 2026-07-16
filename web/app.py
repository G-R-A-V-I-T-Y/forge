import asyncio
import difflib
import json as _json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from market.heartbeat import (
    DEFAULT_HEARTBEAT_PATH,
    heartbeat_max_age_seconds,
    read_heartbeat,
    read_heartbeat_or_none,
)
from agents.reflection import compute_calibration_curve, run_reflection
from meta.controller import evaluate_agent
from meta.evaluator import get_lifecycle_decision, get_null_metrics
from meta.reflection_scheduler import check_agent_eligible, get_reflection_trigger
from store.performance import compute_metrics
from store.query import query_trades, count_trades, get_trade
from store import settings as settings_store
from store.specs import get_spec_history
from store.counterfactuals import get_counterfactual_coverage
from meta.labeling import get_labeling_coverage
from execution.paper_bridge import PaperBridge
from meta.head_of_desk import (
    compose_chat_answer,
    generate_morning_brief,
    get_chat_history,
    run_desk_query,
    save_chat_turn,
    store_briefing,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app = FastAPI(title="Forge")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

logger = logging.getLogger("forge.web")

data_source_map = {"stub": "STUB", "hyperliquid": "LIVE"}


def _build_health_strip(conn, config, provider):
    """Build health strip data for the command deck header."""
    data_source = data_source_map.get(config.get("data_source", "stub"), "STUB") if config else "STUB"
    exchange_ok = provider is not None and getattr(provider._backend, "available", True)

    db_path = Path("data/forge.db")
    db_exists = db_path.exists()

    heartbeat_age = None
    heartbeat_path = (config or {}).get("desk", {}).get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
    packet = read_heartbeat_or_none(heartbeat_path, heartbeat_max_age_seconds(config) if config else 60)
    if packet and packet.get("timestamp"):
        try:
            written_at = datetime.strptime(packet["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            heartbeat_age = round((datetime.now(timezone.utc) - written_at).total_seconds(), 1)
        except (ValueError, TypeError):
            pass

    items = [
        {"id": "data_source", "label": "DATA", "value": data_source, "dot": "ok" if exchange_ok else "error"},
        {"id": "heartbeat", "label": "HB", "value": f"{heartbeat_age}s ago" if heartbeat_age is not None else "—", "dot": "ok" if heartbeat_age is not None and heartbeat_age < 120 else ("degraded" if heartbeat_age is not None else "error")},
        {"id": "db", "label": "DB", "value": f"{round(db_path.stat().st_size / (1024 * 1024), 1) if db_exists else 0} MB", "dot": "ok" if db_exists else "error"},
    ]

    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    active_count = conn.execute("SELECT COUNT(*) FROM agents WHERE status IN ('active','rookie','shadow')").fetchone()[0]
    items.insert(0, {"id": "agents", "label": "AGENTS", "value": f"{active_count}/{agent_count}", "dot": "ok" if active_count > 0 else "error"})

    return items


def _build_activity_feed(conn):
    """Return the last 20 audit_log entries as dicts."""
    rows = conn.execute(
        "SELECT agent_id, action, details_json, performed_by, reason, created_at FROM audit_log ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_evaluation_summaries(conn):
    """Get latest evaluation per agent as a dict keyed by agent_id."""
    rows = conn.execute("""
        SELECT e.* FROM evaluations e
        INNER JOIN (
            SELECT agent_id, MAX(evaluated_at) AS max_at
            FROM evaluations GROUP BY agent_id
        ) latest ON e.agent_id = latest.agent_id AND e.evaluated_at = latest.max_at
    """).fetchall()
    result = {}
    for r in rows:
        result[r["agent_id"]] = dict(r)
    return result


def _resolve_model_used(conn, agent_id: str, last_model_used: str | None) -> str | None:
    """Resolve the display model for a trader: prefer last_model_used from the
    agents table, falling back to the model used on the agent's most recent
    trade (trades.model_used). This ensures the column shows meaningful data
    even when the agent-level field hasn't been populated yet (e.g. after a
    fresh checkout that replaced the tracked database)."""
    if last_model_used:
        return last_model_used
    row = conn.execute(
        "SELECT model_used FROM trades WHERE agent_id = ? AND model_used IS NOT NULL ORDER BY entry_timestamp DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return row["model_used"] if row else None


def _spec_diff(spec_history: list[dict]) -> dict:
    """Build a unified diff between the two most recent spec versions.

    ``spec_history`` is the newest-first list returned by
    ``store.specs.get_spec_history`` (each row includes ``yaml_text``).
    Returns a dict describing what to render: ``available`` is False when
    there are fewer than two versions to compare (a "no diff to show" state),
    otherwise ``lines`` holds the unified diff (as a list of {text, kind}
    dicts so the template can color +/- lines) plus the two version numbers
    being compared.
    """
    if len(spec_history) < 2:
        return {"available": False, "lines": [], "from_version": None, "to_version": None}

    newer, older = spec_history[0], spec_history[1]
    older_text = (older.get("yaml_text") or "").splitlines(keepends=True)
    newer_text = (newer.get("yaml_text") or "").splitlines(keepends=True)

    diff = difflib.unified_diff(
        older_text,
        newer_text,
        fromfile=f"spec_v{older['spec_version']}",
        tofile=f"spec_v{newer['spec_version']}",
    )

    lines = []
    for line in diff:
        stripped = line.rstrip("\n")
        if line.startswith("+++") or line.startswith("---"):
            kind = "header"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "remove"
        else:
            kind = "context"
        lines.append({"text": stripped, "kind": kind})

    return {
        "available": True,
        "lines": lines,
        "from_version": older["spec_version"],
        "to_version": newer["spec_version"],
    }


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    conn = app.state.conn
    provider = getattr(app.state, "provider", None)
    config = getattr(app.state, "config", None)
    data_source = (
        data_source_map.get(config.get("data_source", "stub"), "STUB")
        if config
        else "STUB"
    )
    exchange_ok = provider is not None and getattr(provider._backend, "available", True)

    agent_rows = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
    agents = []
    for row in agent_rows:
        agent = dict(row)
        aid = agent["id"]
        account = conn.execute(
            "SELECT * FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
            (aid,),
        ).fetchone()
        metrics = compute_metrics(conn, aid)
        pos_count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE agent_id = ?", (aid,)
        ).fetchone()[0]
        bal = account["balance"] if account else 50000.0
        peak = account["peak_balance"] if account else 50000.0
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
            }
        )

    trades = conn.execute(
        "SELECT * FROM trades ORDER BY entry_timestamp DESC LIMIT 10"
    ).fetchall()
    trades_list = [dict(t) for t in trades]
    if config:
        heartbeat = read_heartbeat_or_none(DEFAULT_HEARTBEAT_PATH, heartbeat_max_age_seconds(config))
        if heartbeat:
            assets_data = heartbeat.get("assets", {})
            for t in trades_list:
                if t.get("status") == "open":
                    asset = t.get("asset")
                    direction = t.get("direction")
                    entry = t.get("entry_price")
                    leverage = t.get("leverage") or 1
                    asset_data = assets_data.get(asset)
                    if asset_data and entry:
                        current_price = asset_data.get("price")
                        if current_price:
                            if direction == "long":
                                pnl = (current_price - entry) / entry * leverage
                            else:
                                pnl = (entry - current_price) / entry * leverage
                            t["pnl_pct"] = pnl

    positions = conn.execute(
        "SELECT positions.*, trades.model_used AS model_used "
        "FROM positions LEFT JOIN trades ON trades.id = positions.trade_id "
        "ORDER BY positions.agent_id, positions.opened_at"
    ).fetchall()
    total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    # Latest Head-of-Desk morning brief (M9 crit 8) for the overview panel.
    latest_briefing_text = None
    latest_briefing_date = None
    try:
        row = conn.execute(
            "SELECT date, content FROM briefings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            latest_briefing_date = row["date"]
            latest_briefing_text = _json.loads(row["content"]).get("briefing_text")
    except Exception:
        pass

    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "active_page": "overview",
            "agents": agents,
            "trades": trades_list,
            "positions": [dict(p) for p in positions],
            "total_trades": total_trades,
            "data_source": data_source,
            "exchange_ok": exchange_ok,
            "health_items": _build_health_strip(conn, config, provider),
            "activity": _build_activity_feed(conn),
            "evaluation_summaries": _get_evaluation_summaries(conn),
            "latest_briefing_text": latest_briefing_text,
            "latest_briefing_date": latest_briefing_date,
        },
    )


@app.get("/health")
async def health():
    conn = app.state.conn
    provider = getattr(app.state, "provider", None)
    config = getattr(app.state, "config", None)
    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    data_source = config.get("data_source", "stub") if config else "stub"
    exchange_available = provider is not None and getattr(
        provider._backend, "available", True
    )

    db_path = Path("data/forge.db")
    db_size_mb = (
        round(db_path.stat().st_size / (1024 * 1024), 2) if db_path.exists() else 0.0
    )

    heartbeat_path = (config or {}).get("desk", {}).get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
    heartbeat_age_seconds = None
    packet = read_heartbeat(heartbeat_path)
    if packet and packet.get("timestamp"):
        try:
            written_at = datetime.strptime(
                packet["timestamp"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            heartbeat_age_seconds = round(
                (datetime.now(timezone.utc) - written_at).total_seconds(), 1
            )
        except (ValueError, TypeError):
            heartbeat_age_seconds = None

    try:
        from store.counterfactuals import get_counterfactual_coverage as _get_coverage
        counterfactual_coverage = _get_coverage(conn)
    except Exception:
        counterfactual_coverage = {}

    return {
        "status": "ok" if exchange_available else "degraded",
        "agents": agent_count,
        "trades": trade_count,
        "data_source": data_source,
        "exchange_available": exchange_available,
        "db_size_mb": db_size_mb,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "counterfactual_coverage": counterfactual_coverage,
    }


@app.get("/api/prices")
async def get_prices():
    """Reads asset prices from the shared heartbeat file instead of calling
    provider.get_all_mids() live on every client poll — see
    docs/superpowers/specs/2026-07-01-heartbeat-wiring-design.md. Returns {}
    (unchanged failure-mode contract) if the heartbeat is missing/stale."""
    config = getattr(app.state, "config", None)
    if config is None:
        return {}
    desk_config = config.get("desk", {})
    heartbeat_path = desk_config.get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
    universe = config.get("universe", [])
    heartbeat = read_heartbeat_or_none(heartbeat_path, heartbeat_max_age_seconds(config))
    if heartbeat is None:
        return {}
    assets_data = heartbeat.get("assets", {})
    result = {}
    for asset in universe:
        price = assets_data.get(asset, {}).get("price")
        if price is not None:
            result[asset] = price
    return result


PAGE_SIZE = 25


def _trade_filters(agent, asset, direction, outcome, regime, date_from, date_to):
    return {
        "agent_id": agent or None,
        "asset": asset or None,
        "direction": direction or None,
        "outcome": outcome or None,
        "regime": regime or None,
        "date_from": date_from or None,
        "date_to": date_to or None,
    }


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(
    request: Request,
    agent: str = "",
    asset: str = "",
    direction: str = "",
    outcome: str = "",
    regime: str = "",
    page: int = 1,
):
    conn = app.state.conn
    page = max(1, page)
    filters = _trade_filters(agent, asset, direction, outcome, regime, None, None)

    total = count_trades(conn, **filters)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE

    trades = query_trades(
        conn, decode_ohlcv=False, limit=PAGE_SIZE, offset=offset, **filters
    )

    agents = [r["id"] for r in conn.execute("SELECT id FROM agents ORDER BY id")]
    assets = [
        r[0] for r in conn.execute("SELECT DISTINCT asset FROM trades ORDER BY asset")
    ]
    regimes = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT regime FROM trades WHERE regime IS NOT NULL ORDER BY regime"
        )
    ]

    return templates.TemplateResponse(
        "trade_bank.html",
        {
            "request": request,
            "trades": trades,
            "agents": agents,
            "assets": assets,
            "regimes": regimes,
            "filters": {
                "agent": agent,
                "asset": asset,
                "direction": direction,
                "outcome": outcome,
                "regime": regime,
            },
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@app.get("/decisions", response_class=HTMLResponse)
async def decisions_page(request: Request):
    conn = app.state.conn

    # T7 (M10 gap closure): one generalized coverage surface. The headline
    # is forward-labeling coverage (meta/labeling.py's nightly job, which
    # now also absorbs the M6 wait-only counterfactual filler); the legacy
    # wait-counterfactual fill stats are folded in as secondary stats
    # inside the same dict rather than rendered as a second panel.
    coverage = get_labeling_coverage(conn)
    coverage["counterfactual"] = get_counterfactual_coverage(conn)

    # All decisions grouped by agent
    agents = conn.execute("SELECT id FROM agents ORDER BY id").fetchall()
    decisions_by_agent = {}
    for row in agents:
        aid = row["id"]
        rows = conn.execute(
            """SELECT id, timestamp, decision_action, decision_reason,
                      counterfactual_result, counterfactual_was_better
               FROM decisions
               WHERE agent_id = ?
               ORDER BY timestamp DESC
               LIMIT 20""",
            (aid,),
        ).fetchall()
        decisions_by_agent[aid] = [dict(r) for r in rows]

    # Recent hypotheses. The hypotheses table has no `summary` column (it
    # has `claim`) -- a bare `except: pass` previously swallowed the
    # resulting sqlite3.OperationalError on every request, so the panel
    # silently rendered empty. Column list fixed; unexpected errors are now
    # logged instead of hidden.
    hypotheses = []
    try:
        rows = conn.execute(
            "SELECT id, agent_id, created_at, status, claim FROM hypotheses ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        hypotheses = [dict(r) for r in rows]
    except Exception:
        logger.warning("Failed to query hypotheses for /decisions", exc_info=True)

    return templates.TemplateResponse(
        "decisions.html",
        {
            "request": request,
            "active_page": "decisions",
            "coverage": coverage,
            "decisions_by_agent": decisions_by_agent,
            "hypotheses": hypotheses,
            "agents": [r["id"] for r in agents],
        },
    )


@app.get("/api/query")
async def api_query(
    agent: str | None = None,
    asset: str | None = None,
    direction: str | None = None,
    outcome: str | None = None,
    regime: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    funding_rate_min: float | None = None,
    funding_rate_max: float | None = None,
    oi_change_min: float | None = None,
    oi_change_max: float | None = None,
    limit: int = 200,
    offset: int = 0,
    include_ohlcv: bool = False,
):
    """Cross-agent trade bank query, e.g. for the Head of Desk chat (M7).

    agent=None searches every agent. OHLCV blobs are omitted by default to
    keep list responses small; pass include_ohlcv=true to decode them.
    """
    conn = app.state.conn
    trades = query_trades(
        conn,
        agent_id=agent,
        asset=asset,
        direction=direction,
        regime=regime,
        outcome=outcome,
        status=status,
        date_from=date_from,
        date_to=date_to,
        funding_rate_min=funding_rate_min,
        funding_rate_max=funding_rate_max,
        oi_change_min=oi_change_min,
        oi_change_max=oi_change_max,
        limit=limit,
        offset=offset,
        decode_ohlcv=include_ohlcv,
    )
    return JSONResponse(trades)


@app.post("/api/query")
async def api_query_post(request: Request):
    """POST /api/query — accepts JSON filter params, returns filtered trades.
    Used by the Head of Desk chat (future milestone) and for programmatic access.
    """
    conn = app.state.conn
    body = (
        await request.json()
        if request.headers.get("content-type") == "application/json"
        else {}
    )
    trades = query_trades(
        conn,
        agent_id=body.get("agent"),
        asset=body.get("asset"),
        direction=body.get("direction"),
        regime=body.get("regime"),
        outcome=body.get("outcome"),
        status=body.get("status"),
        date_from=body.get("date_from"),
        date_to=body.get("date_to"),
        funding_rate_min=body.get("funding_rate_min"),
        funding_rate_max=body.get("funding_rate_max"),
        oi_change_min=body.get("oi_change_min"),
        oi_change_max=body.get("oi_change_max"),
        limit=body.get("limit", 200),
        offset=body.get("offset", 0),
        decode_ohlcv=body.get("include_ohlcv", False),
    )
    return JSONResponse(trades)


@app.get("/api/desk")
async def api_desk():
    """Returns JSON summary of all agents' current state for the leaderboard."""
    conn = app.state.conn
    rows = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
    agents = []
    for row in rows:
        agent = dict(row)
        aid = agent["id"]

        account = conn.execute(
            "SELECT * FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
            (aid,),
        ).fetchone()

        balance = account["balance"] if account else 50000.0
        peak = account["peak_balance"] if account else 50000.0

        metrics = compute_metrics(conn, aid)

        pos_count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE agent_id = ?", (aid,)
        ).fetchone()[0]

        weekly_return = metrics.get("last_7d_return", 0.0)

        agents.append(
            {
                "name": aid,
                "status": agent["status"],
                "balance": round(balance, 2),
                "trades_count": metrics["closed_trades"],
                "win_rate": round(metrics["win_rate"], 4),
                "profit_factor": round(metrics["profit_factor"], 4)
                if metrics["profit_factor"] != float("inf")
                else 0.0,
                "sharpe": round(metrics["sharpe"], 4),
                "weekly_return": round(weekly_return, 4),
                "max_drawdown": round((peak - balance) / peak, 4) if peak > 0 else 0.0,
                "open_positions_count": pos_count,
                "last_model_used": _resolve_model_used(conn, aid, agent.get("last_model_used")),
            }
        )
    return agents


@app.get("/agents/{name}", response_class=HTMLResponse)
async def agent_detail(request: Request, name: str):
    conn = app.state.conn
    provider = getattr(app.state, "provider", None)
    config = getattr(app.state, "config", None)
    data_source = (
        data_source_map.get(config.get("data_source", "stub"), "STUB")
        if config
        else "STUB"
    )
    exchange_ok = provider is not None and getattr(provider._backend, "available", True)

    agent = conn.execute("SELECT * FROM agents WHERE id = ?", (name,)).fetchone()
    if not agent:
        return HTMLResponse("Agent not found", status_code=404)

    agent = dict(agent)
    aid = agent["id"]

    account = conn.execute(
        "SELECT * FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
        (aid,),
    ).fetchone()
    account_dict = (
        dict(account) if account else {"balance": 50000.0, "peak_balance": 50000.0}
    )
    peak = account_dict["peak_balance"]
    max_dd = (peak - account_dict["balance"]) / peak if peak > 0 else 0.0

    metrics = compute_metrics(conn, aid)

    open_positions = conn.execute(
        "SELECT positions.*, trades.model_used AS model_used "
        "FROM positions LEFT JOIN trades ON trades.id = positions.trade_id "
        "WHERE positions.agent_id = ?",
        (aid,),
    ).fetchall()

    trade_history = conn.execute(
        "SELECT * FROM trades WHERE agent_id = ? ORDER BY entry_timestamp DESC LIMIT 50",
        (aid,),
    ).fetchall()

    thesis_version = agent.get("current_thesis_version", 1)
    thesis_path = Path("agents/theses") / f"{aid}_v{thesis_version}.md"
    try:
        thesis_text = thesis_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        thesis_text = "Thesis file not found."

    spec_history = get_spec_history(conn, aid)
    spec_diff = _spec_diff(spec_history)
    calibration = compute_calibration_curve(conn, aid)

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
        },
    )


@app.get("/api/agents/{name}")
async def api_agent_detail(name: str):
    conn = app.state.conn
    agent = conn.execute("SELECT * FROM agents WHERE id = ?", (name,)).fetchone()
    if not agent:
        return JSONResponse({"error": "not found"}, status_code=404)
    aid = agent["id"]
    account = conn.execute(
        "SELECT * FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
        (aid,),
    ).fetchone()
    metrics = compute_metrics(conn, aid)
    pos_count = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE agent_id = ?", (aid,)
    ).fetchone()[0]
    return {
        "name": aid,
        "status": agent["status"],
        "spawn_date": agent["spawn_date"],
        "metrics": {
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "sharpe": metrics["sharpe"],
            "closed_trades": metrics["closed_trades"],
            "last_7d_return": metrics.get("last_7d_return", 0.0),
        },
        "balance": account["balance"] if account else 50000.0,
        "open_positions_count": pos_count,
        "last_model_used": agent["last_model_used"] if "last_model_used" in agent.keys() else None,
    }


@app.post("/api/positions/{position_id}/close")
async def api_close_position(position_id: str):
    """Manually close an open position (button on the overview page).

    Races with SL/TP and agent-driven closes are possible — if the position
    is already gone by the time this runs, PaperBridge.close() returns {}
    and we surface that as a 404 rather than crashing.
    """
    conn = app.state.conn
    provider = getattr(app.state, "provider", None)
    config = getattr(app.state, "config", None)

    row = conn.execute(
        "SELECT agent_id FROM positions WHERE id = ?", (position_id,)
    ).fetchone()
    if not row:
        return JSONResponse({"error": "position not found"}, status_code=404)
    agent_id = row["agent_id"]

    bridge = PaperBridge(
        agent_id=agent_id, conn=conn, provider=provider, config=config
    )
    result = await bridge.close(position_id, reason="manual_close")
    if not result:
        return JSONResponse({"error": "position not found"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/trades/{trade_id}")
async def api_trade_detail(trade_id: str):
    """Full fingerprint for one trade, including decoded OHLCV — used by the
    /trades page to render the candlestick chart on row expand."""
    conn = app.state.conn
    trade = get_trade(conn, trade_id, decode_ohlcv=True)
    if trade is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(trade)


def _audit(conn, action: str, agent_id: str | None, reason: str | None, details: str | None = None):
    conn.execute(
        "INSERT INTO audit_log (agent_id, action, details_json, performed_by, reason, created_at) VALUES (?, ?, ?, 'human', ?, ?)",
        (agent_id, action, details, reason, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.commit()


@app.post("/api/exec/trigger-reflection/{agent_id}")
async def exec_trigger_reflection(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    config = getattr(app.state, "config", None) or {}
    agent = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    agent_config = conn.execute("SELECT config_json FROM agents WHERE id = ?", (agent_id,)).fetchone()
    agent_cfg = _json.loads(agent_config["config_json"]) if agent_config and agent_config["config_json"] else {}
    llm_fn = getattr(app.state, "llm_fn", None)
    if llm_fn is None:
        return JSONResponse({"error": "LLM function not available"}, status_code=503)

    run_reflection(conn, agent_id, config, llm_fn)
    _audit(conn, "trigger_reflection", agent_id, reason)
    return {"ok": True, "detail": f"Reflection triggered for {agent_id}"}


@app.post("/api/exec/trigger-all-reflections")
async def exec_trigger_all_reflections(reason: str = Query(...)):
    """Enqueue a reflection cycle for every candidate agent that
    check_agent_eligible clears (M9 criterion 3). Runs each eligible agent's
    reflection inline via app.state.llm_fn -- the same execution style as
    the single-agent /api/exec/trigger-reflection/{agent_id} endpoint above,
    which is also a direct synchronous call, not a background task."""
    conn = app.state.conn
    config = getattr(app.state, "config", None) or {}
    llm_fn = getattr(app.state, "llm_fn", None)
    if llm_fn is None:
        return JSONResponse({"error": "LLM function not available"}, status_code=503)

    trigger = get_reflection_trigger(conn)
    agents = conn.execute(
        "SELECT id FROM agents WHERE status IN ('active','rookie','shadow') ORDER BY name"
    ).fetchall()

    results = []
    for row in agents:
        aid = row["id"]
        eligible, ineligible_reason = check_agent_eligible(conn, aid, trigger)
        if not eligible:
            results.append({"agent_id": aid, "status": "skipped", "reason": ineligible_reason})
            continue
        try:
            run_reflection(conn, aid, config, llm_fn)
            _audit(conn, "trigger_reflection", aid, reason)
            results.append({"agent_id": aid, "status": "queued"})
        except Exception as exc:
            logger.warning("Reflection failed for %s: %s", aid, exc)
            results.append({"agent_id": aid, "status": "skipped", "reason": str(exc)})

    return {"ok": True, "results": results}


@app.post("/api/exec/trigger-evaluation/{agent_id}")
async def exec_trigger_evaluation(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    agent = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    metrics = compute_metrics(conn, agent_id)
    null_metrics = get_null_metrics(conn)
    decision = get_lifecycle_decision(conn, agent_id, metrics, null_metrics)

    conn.execute(
        "INSERT INTO evaluations (agent_id, evaluated_at, trades_evaluated, metrics_json, decision, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (
            agent_id,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            metrics.get("closed_trades", 0),
            _json.dumps(metrics),
            decision.get("decision", "review"),
            decision.get("reason", ""),
        ),
    )
    conn.commit()
    _audit(conn, "trigger_evaluation", agent_id, reason)
    return {"ok": True, "decision": decision}


@app.post("/api/exec/trigger-all-evaluations")
async def exec_trigger_all_evaluations(reason: str = Query(...)):
    """Force an evaluation cycle for every active/rookie agent (M9 criterion
    3), via the single meta/controller.py::evaluate_agent code path -- same
    metrics/null-comparison/lifecycle-decision/harvest logic as the
    scheduled cycle, just with the interval-due gate bypassed."""
    conn = app.state.conn
    agents = conn.execute(
        "SELECT id FROM agents WHERE status IN ('active','rookie') AND id NOT LIKE 'benchmark_%' ORDER BY name"
    ).fetchall()

    results = []
    for row in agents:
        aid = row["id"]
        try:
            result = evaluate_agent(conn, aid, force=True)
            _audit(conn, "trigger_evaluation", aid, reason)
            results.append(result)
        except Exception as exc:
            logger.warning("Evaluation failed for %s: %s", aid, exc)
            results.append({"agent_id": aid, "error": str(exc)})

    return {"ok": True, "results": results, "count": len(results)}


@app.post("/api/exec/disable-entries/{agent_id}")
async def exec_disable_entries(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    agent = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason, enabled_at) VALUES (?, 'human', ?, ?, NULL)",
        (agent_id, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), reason),
    )
    conn.commit()
    _audit(conn, "disable_entries", agent_id, reason)
    return {"ok": True}


@app.post("/api/exec/enable-entries/{agent_id}")
async def exec_enable_entries(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    agent = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    conn.execute(
        "UPDATE entry_disables SET enabled_at = ? WHERE agent_id = ? AND enabled_at IS NULL",
        (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), agent_id),
    )
    conn.commit()
    _audit(conn, "enable_entries", agent_id, reason)
    return {"ok": True}


@app.post("/api/exec/demote-agent/{agent_id}")
async def exec_demote_agent(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    agent = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    conn.execute("UPDATE agents SET status = 'suspended' WHERE id = ?", (agent_id,))
    conn.commit()
    _audit(conn, "demote_agent", agent_id, reason)
    return {"ok": True, "status": "suspended"}


@app.post("/api/exec/promote-shadow/{agent_id}")
async def exec_promote_shadow(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    agent = conn.execute("SELECT id, status FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    if agent["status"] not in ("active", "rookie", "suspended"):
        return JSONResponse({"error": "Agent not eligible"}, status_code=400)

    conn.execute("UPDATE agents SET status = 'shadow' WHERE id = ?", (agent_id,))
    conn.commit()
    _audit(conn, "promote_shadow", agent_id, reason)
    return {"ok": True, "status": "shadow"}


@app.post("/api/exec/go-live/{agent_id}")
async def exec_go_live(agent_id: str, reason: str = Query(...)):
    conn = app.state.conn
    agent = conn.execute("SELECT id, status FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    if agent["status"] != "shadow":
        return JSONResponse({"error": "Agent must be in shadow status first"}, status_code=400)

    conn.execute("UPDATE agents SET status = 'live' WHERE id = ?", (agent_id,))
    conn.commit()
    _audit(conn, "go_live", agent_id, reason)
    return {"ok": True, "status": "live"}


@app.post("/api/exec/emergency-stop")
async def exec_emergency_stop(reason: str = Query(...)):
    conn = app.state.conn
    agent_count = conn.execute("SELECT COUNT(*) FROM agents WHERE status != 'suspended'").fetchone()[0]
    conn.execute("UPDATE agents SET status = 'suspended'")
    conn.commit()
    _audit(conn, "emergency_stop", None, reason)
    return {"ok": True, "agents_affected": agent_count}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    conn = app.state.conn
    current = settings_store.load_all(conn)
    llama_srv = getattr(app.state, "llama_server", None)
    server_status = llama_srv.status() if llama_srv else {"running": False, "pid": None}
    agent_names = [r["id"] for r in conn.execute("SELECT id FROM agents ORDER BY id")]
    evaluation_settings = {
        "eval_interval_minutes": 60,
        "reflection_interval_default": 10,
        "max_drawdown_threshold": 0.15,
        "min_win_rate_threshold": 0.40,
        "max_concentration_pct": 0.30,
        "daily_loss_limit": 0.05,
    }
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_page": "settings",
            "settings": current,
            "all_settings": current,
            "server_status": server_status,
            "min_context_size": settings_store.MIN_CONTEXT_SIZE,
            "agent_names": agent_names,
            "evaluation_settings": evaluation_settings,
        },
    )


@app.get("/api/settings")
async def api_get_settings():
    conn = app.state.conn
    return JSONResponse(settings_store.load_all(conn))


@app.post("/api/settings")
async def api_save_settings(request: Request):
    """Persist settings and restart the local server if it was running."""
    conn = app.state.conn
    body = await request.json()

    # Coerce numeric fields.
    for int_key in ("context_size", "batch_size", "ubatch_size", "threads",
                    "gpu_layers", "n_cpu_moe", "llama_server_port"):
        if int_key in body:
            try:
                body[int_key] = int(body[int_key])
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": f"{int_key} must be an integer"}, status_code=422
                )

    errors = settings_store.validate_server_settings(body)
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    settings_store.save_all(conn, body)

    # Restart the local server so new settings take effect immediately.
    llama_srv = getattr(app.state, "llama_server", None)
    if llama_srv is not None and llama_srv.is_running():
        all_settings = settings_store.load_all(conn)
        llama_srv.restart(all_settings)

    return JSONResponse({"ok": True})


@app.get("/api/local-server/status")
async def api_local_server_status():
    llama_srv = getattr(app.state, "llama_server", None)
    if llama_srv is None:
        return JSONResponse({"running": False, "pid": None})
    return JSONResponse(llama_srv.status())


@app.post("/api/local-server/start")
async def api_local_server_start():
    conn = app.state.conn
    llama_srv = getattr(app.state, "llama_server", None)
    if llama_srv is None:
        return JSONResponse({"error": "server manager not available"}, status_code=503)
    all_settings = settings_store.load_all(conn)
    ok = llama_srv.start(all_settings)
    if ok:
        return JSONResponse({"ok": True, "status": llama_srv.status()})
    return JSONResponse({"error": "failed to start; check logs"}, status_code=500)


@app.post("/api/local-server/stop")
async def api_local_server_stop():
    llama_srv = getattr(app.state, "llama_server", None)
    if llama_srv is None:
        return JSONResponse({"error": "server manager not available"}, status_code=503)
    llama_srv.stop()
    return JSONResponse({"ok": True, "status": llama_srv.status()})


@app.get("/api/agents/{name}/balance-history")
async def api_agent_balance_history(name: str):
    """Returns historical account balance data for a single agent,
    used by the leaderboard sparkline."""
    conn = app.state.conn
    rows = conn.execute(
        "SELECT a.balance, a.recorded_at FROM accounts a "
        "JOIN agents ag ON a.agent_id = ag.id "
        "WHERE ag.name = ? AND a.mode = 'paper' "
        "ORDER BY a.recorded_at DESC LIMIT 30",
        (name,),
    ).fetchall()
    return [{"balance": r["balance"], "recorded_at": r["recorded_at"]} for r in rows]


@app.get("/api/agents/balance-history")
async def api_all_balance_history():
    """Returns historical account balance data for all agents,
    keyed by agent name (matching WebSocket desk data)."""
    conn = app.state.conn
    agents = conn.execute(
        "SELECT id, name FROM agents ORDER BY name"
    ).fetchall()
    agent_map = {a["id"]: a["name"] for a in agents}
    rows = conn.execute(
        "SELECT agent_id, balance, recorded_at FROM accounts "
        "WHERE mode = 'paper' "
        "ORDER BY agent_id, recorded_at DESC"
    ).fetchall()
    result = {name: [] for name in agent_map.values()}
    for row in rows:
        name = agent_map.get(row["agent_id"])
        if name and len(result[name]) < 30:
            result[name].append({
                "balance": row["balance"],
                "recorded_at": row["recorded_at"]
            })
    return result


@app.websocket("/api/ws/desk")
async def ws_desk(websocket: WebSocket):
    """Broadcast desk state summary every 30 seconds to connected clients."""
    await websocket.accept()
    conn = app.state.conn
    try:
        while True:
            rows = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
            agents = []
            for row in rows:
                agent = dict(row)
                aid = agent["id"]
                account = conn.execute(
                    "SELECT * FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
                    (aid,),
                ).fetchone()
                balance = account["balance"] if account else 50000.0
                peak = account["peak_balance"] if account else 50000.0
                metrics = compute_metrics(conn, aid)
                pos_count = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE agent_id = ?", (aid,)
                ).fetchone()[0]
                agents.append(
                    {
                        "name": aid,
                        "status": agent["status"],
                        "balance": round(balance, 2),
                        "trades_count": metrics["closed_trades"],
                        "win_rate": round(metrics["win_rate"], 4),
                        "profit_factor": round(metrics["profit_factor"], 4)
                        if metrics["profit_factor"] != float("inf")
                        else 0.0,
                        "sharpe": round(metrics["sharpe"], 4),
                        "weekly_return": round(metrics.get("last_7d_return", 0.0), 4),
                        "max_drawdown": round((peak - balance) / peak, 4)
                        if peak > 0
                        else 0.0,
                        "open_positions_count": pos_count,
                        "last_model_used": _resolve_model_used(conn, aid, agent.get("last_model_used")),
                    }
                )
            await websocket.send_json(agents)
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    conn = app.state.conn
    try:
        row = conn.execute(
            "SELECT content FROM briefings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_briefing = row["content"] if row else None
    except Exception:
        latest_briefing = None
    try:
        history = get_chat_history(conn, limit=50)
    except Exception:
        history = []
    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "active_page": "chat",
            "latest_briefing": latest_briefing,
            "chat_history": history,
        },
    )


@app.get("/api/briefing/latest")
async def api_briefing_latest():
    conn = app.state.conn
    try:
        row = conn.execute(
            "SELECT * FROM briefings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return JSONResponse({"briefing": None})
        return JSONResponse({
            "briefing": {
                "date": row["date"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/briefing/generate")
async def api_briefing_generate():
    conn = app.state.conn
    config = getattr(app.state, "config", None)
    brief = generate_morning_brief(conn, config)
    store_briefing(conn, brief)
    return JSONResponse({
        "ok": True,
        "briefing_text": brief["briefing_text"],
        "date": brief["date"],
        "agents_covered": brief["agents_covered"],
        "summary": brief["summary"],
    })


@app.websocket("/api/ws/chat")
async def ws_chat(websocket: WebSocket):
    """Head-of-Desk chat (M9 crit 8).

    Protocol: client sends {"query": <text>}; server streams the answer as
    {"role": "assistant", "chunk": <text>} frames followed by a terminal
    {"role": "assistant", "done": true, "content": <full answer>}. Turns
    (user + assistant) are persisted to chat_history. Answers are composed
    by the reflection-transport LLM over query-tool results when
    app.state.llm_fn is wired (forge.py startup), with a structured
    non-LLM fallback otherwise.
    """
    await websocket.accept()
    conn = app.state.conn
    llm_fn = getattr(app.state, "llm_fn", None)
    try:
        while True:
            data = await websocket.receive_json()
            query_text = data.get("query", "").strip()
            if not query_text:
                await websocket.send_json(
                    {"role": "assistant", "done": True, "content": "Please enter a query."}
                )
                continue

            save_chat_turn(conn, "user", query_text)
            result = await run_in_threadpool(
                compose_chat_answer, conn, query_text, llm_fn
            )

            # Stream in line chunks so long answers render progressively.
            for line in result.splitlines(keepends=True):
                await websocket.send_json({"role": "assistant", "chunk": line})
            await websocket.send_json(
                {"role": "assistant", "done": True, "content": result}
            )
            save_chat_turn(conn, "assistant", result)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json(
                {"role": "assistant", "done": True, "content": f"Error: {exc}"}
            )
        except Exception:
            pass
