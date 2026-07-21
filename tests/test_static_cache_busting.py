"""Static assets must be cache-busted with a version query string.

Without it, Starlette's StaticFiles serves /static/forge.js with no
Cache-Control header, so browsers apply heuristic freshness and keep a
stale cached copy after the file changes on disk.  A stale forge.js is
missing renderPortfolioEquityChart/renderAgentEquityChart, which makes
the inline page scripts throw and the equity charts silently vanish on
the overview and trader pages.
"""
from fastapi.testclient import TestClient

from store.db import insert_agent
from web.app import app, static_url

AGENT_ID = "jade_hawk"


def _client(conn) -> TestClient:
    app.state.conn = conn
    app.state.provider = None
    app.state.config = {}
    return TestClient(app)


def test_static_url_appends_mtime_version():
    url = static_url("forge.js")
    assert url.startswith("/static/forge.js?v=")
    assert url.split("v=")[1].isdigit()


def test_static_url_stable_within_process():
    assert static_url("forge.js") == static_url("forge.js")


def test_overview_uses_versioned_static_assets(conn):
    r = _client(conn).get("/")
    assert r.status_code == 200
    assert "/static/forge.js?v=" in r.text
    assert "/static/forge.css?v=" in r.text
    # No unversioned includes left behind
    assert 'src="/static/forge.js"' not in r.text
    assert 'href="/static/forge.css"' not in r.text


def test_agent_detail_uses_versioned_forge_js(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "/static/forge.js?v=" in r.text
    assert 'src="/static/forge.js"' not in r.text


def test_trade_bank_uses_versioned_forge_js(conn):
    r = _client(conn).get("/trades")
    assert r.status_code == 200
    assert "/static/forge.js?v=" in r.text
    assert 'src="/static/forge.js"' not in r.text
