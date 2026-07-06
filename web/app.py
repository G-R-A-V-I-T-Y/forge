import asyncio
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from market.heartbeat import (
    DEFAULT_HEARTBEAT_PATH,
    heartbeat_max_age_seconds,
    read_heartbeat,
    read_heartbeat_or_none,
)
from store.performance import compute_metrics
from store.query import query_trades, count_trades, get_trade
from store import settings as settings_store
from execution.paper_bridge import PaperBridge

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app = FastAPI(title="Forge")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

logger = logging.getLogger("forge.web")

data_source_map = {"stub": "STUB", "hyperliquid": "LIVE"}


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
    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "agents": agents,
            "trades": trades_list,
            "positions": [dict(p) for p in positions],
            "total_trades": total_trades,
            "data_source": data_source,
            "exchange_ok": exchange_ok,
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

    return {
        "status": "ok" if exchange_available else "degraded",
        "agents": agent_count,
        "trades": trade_count,
        "data_source": data_source,
        "exchange_available": exchange_available,
        "db_size_mb": db_size_mb,
        "heartbeat_age_seconds": heartbeat_age_seconds,
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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    conn = app.state.conn
    current = settings_store.load_all(conn)
    llama_srv = getattr(app.state, "llama_server", None)
    server_status = llama_srv.status() if llama_srv else {"running": False, "pid": None}
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": current,
            "server_status": server_status,
            "min_context_size": settings_store.MIN_CONTEXT_SIZE,
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
                    "gpu_layers", "llama_server_port"):
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
    agents = conn.execute("SELECT id, name FROM agents ORDER BY name").fetchall()
    result = {}
    for agent in agents:
        aid = agent["id"]
        name = agent["name"]
        rows = conn.execute(
            "SELECT balance, recorded_at FROM accounts "
            "WHERE agent_id = ? AND mode = 'paper' "
            "ORDER BY recorded_at DESC LIMIT 30",
            (aid,),
        ).fetchall()
        result[name] = [
            {"balance": r["balance"], "recorded_at": r["recorded_at"]} for r in rows
        ]
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
