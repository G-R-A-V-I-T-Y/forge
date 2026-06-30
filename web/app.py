from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app = FastAPI(title="Forge")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def overview(request: Request):
    conn = app.state.conn
    # Query agent 'jade_hawk'
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
    })


@app.get("/health")
async def health():
    conn = app.state.conn
    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return {"status": "ok", "agents": agent_count, "trades": trade_count}
