import asyncio
import json
import logging
import math
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from store.query import query_trades, count_trades, get_trade
from execution.paper_bridge import PaperBridge

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app = FastAPI(title="Forge")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

logger = logging.getLogger("forge.web")

data_source_map = {"stub": "STUB", "hyperliquid": "LIVE"}

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    conn = app.state.conn
    provider = getattr(app.state, "provider", None)
    config = getattr(app.state, "config", None)
    data_source = data_source_map.get(config.get("data_source", "stub"), "STUB") if config else "STUB"
    exchange_ok = provider is not None and getattr(provider._backend, "available", True)

    agent = conn.execute("SELECT * FROM agents WHERE id = ?", ("jade_hawk",)).fetchone()
    account = conn.execute(
        "SELECT * FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
        ("jade_hawk",),
    ).fetchone()
    trades = conn.execute(
        "SELECT * FROM trades WHERE agent_id = ? ORDER BY entry_timestamp DESC LIMIT 10",
        ("jade_hawk",),
    ).fetchall()
    positions = conn.execute(
        "SELECT * FROM positions WHERE agent_id = ?", ("jade_hawk",)
    ).fetchall()
    return templates.TemplateResponse("overview.html", {
        "request": request,
        "agent": dict(agent) if agent else {},
        "account": dict(account) if account else {"balance": 50000.0, "peak_balance": 50000.0},
        "trades": [dict(t) for t in trades],
        "positions": [dict(p) for p in positions],
        "data_source": data_source,
        "exchange_ok": exchange_ok,
    })


@app.get("/health")
async def health():
    conn = app.state.conn
    provider = getattr(app.state, "provider", None)
    config = getattr(app.state, "config", None)
    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    data_source = config.get("data_source", "stub") if config else "stub"
    exchange_available = provider is not None and getattr(provider._backend, "available", True)

    db_path = Path("data/forge.db")
    db_size_mb = round(db_path.stat().st_size / (1024 * 1024), 2) if db_path.exists() else 0.0

    return {
        "status": "ok" if exchange_available else "degraded",
        "agents": agent_count,
        "trades": trade_count,
        "data_source": data_source,
        "exchange_available": exchange_available,
        "db_size_mb": db_size_mb,
    }


@app.get("/api/prices")
async def get_prices():
    provider = getattr(app.state, "provider", None)
    config = getattr(app.state, "config", None)
    if provider is None:
        return {}
    universe = config.get("universe", []) if config else []
    try:
        mids = await provider.get_all_mids()
        result = {}
        for asset in universe:
            coin = asset.replace("-PERP", "")
            mid = mids.get(coin)
            if mid is not None:
                result[asset] = mid
        return result
    except Exception:
        return {}


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

    trades = query_trades(conn, decode_ohlcv=False, limit=PAGE_SIZE, offset=offset, **filters)

    agents = [r["id"] for r in conn.execute("SELECT id FROM agents ORDER BY id")]
    assets = [r[0] for r in conn.execute("SELECT DISTINCT asset FROM trades ORDER BY asset")]
    regimes = [r[0] for r in conn.execute(
        "SELECT DISTINCT regime FROM trades WHERE regime IS NOT NULL ORDER BY regime"
    )]

    return templates.TemplateResponse("trade_bank.html", {
        "request": request,
        "trades": trades,
        "agents": agents,
        "assets": assets,
        "regimes": regimes,
        "filters": {
            "agent": agent, "asset": asset, "direction": direction,
            "outcome": outcome, "regime": regime,
        },
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })


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
        agent_id=agent, asset=asset, direction=direction, regime=regime,
        outcome=outcome, status=status, date_from=date_from, date_to=date_to,
        funding_rate_min=funding_rate_min, funding_rate_max=funding_rate_max,
        oi_change_min=oi_change_min, oi_change_max=oi_change_max,
        limit=limit, offset=offset, decode_ohlcv=include_ohlcv,
    )
    return JSONResponse(trades)


@app.post("/api/query")
async def api_query_post(request: Request):
    """POST /api/query — accepts JSON filter params, returns filtered trades.
    Used by the Head of Desk chat (future milestone) and for programmatic access.
    """
    conn = app.state.conn
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    trades = query_trades(
        conn,
        agent_id=body.get("agent"), asset=body.get("asset"),
        direction=body.get("direction"), regime=body.get("regime"),
        outcome=body.get("outcome"), status=body.get("status"),
        date_from=body.get("date_from"), date_to=body.get("date_to"),
        funding_rate_min=body.get("funding_rate_min"),
        funding_rate_max=body.get("funding_rate_max"),
        oi_change_min=body.get("oi_change_min"),
        oi_change_max=body.get("oi_change_max"),
        limit=body.get("limit", 200), offset=body.get("offset", 0),
        decode_ohlcv=body.get("include_ohlcv", False),
    )
    return JSONResponse(trades)


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

    bridge = PaperBridge(agent_id="jade_hawk", conn=conn, provider=provider, config=config)
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
