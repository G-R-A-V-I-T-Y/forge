import asyncio
import json
import logging
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

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


@app.websocket("/api/prices")
async def prices_websocket(websocket: WebSocket):
    await websocket.accept()
    provider = getattr(app.state, "provider", None)
    if provider is None:
        await websocket.send_json({"error": "no provider"})
        await websocket.close()
        return
    try:
        while True:
            try:
                mids = await provider.get_all_mids()
                enriched = {}
                for coin, mid in mids.items():
                    enriched[f"{coin}-PERP"] = mid
                await websocket.send_json(enriched)
            except Exception:
                await websocket.send_json({})
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
