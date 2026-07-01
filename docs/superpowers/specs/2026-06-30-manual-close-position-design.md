# Manual "Close Position" Button — Design

Approved via brainstorming dialogue with the captain on 2026-06-30.

## Problem

The overview page shows open positions but offers no way for a human to
manually close one. Positions currently only close via SL/TP or an agent
decision during a tick. Operators need a manual override for cases like
stuck positions, risk events, or testing.

## API

Add `POST /api/positions/{position_id}/close` to `web/app.py`. The handler
builds a `PaperBridge` from the existing `app.state.conn`/`app.state.provider`/
`app.state.config` (the same objects the `/` route already reads, following
the hardcoded `agent_id = "jade_hawk"` convention used elsewhere in this
file) and calls `bridge.close(position_id, reason="manual_close")`.

`PaperBridge.close()` already returns `{}` when the position id doesn't
exist — this happens naturally if SL/TP or an agent decision closed the
position in the same tick as the manual click. The route treats an empty
result as a 404 with a JSON error body rather than crashing. On success it
returns the trade result dict as JSON (`trade_id`, `exit_price`, `pnl_pct`,
`pnl_usd`).

No new bridge methods, schema changes, or migrations are needed — this
reuses the M1 `close()` implementation and the M4 fingerprint `outcome`
fields (`exit_reason` stores the literal string `"manual_close"`).

## UI

Add a "Close" button to each row of the "Open Positions" table in
`web/templates/overview.html`. The position `id` is already present on each
row (`positions` is loaded via `SELECT * FROM positions WHERE agent_id = ?`
in the `/` route), so no route/query changes are required.

Clicking "Close" runs a `confirm()` dialog identifying the position (asset +
direction + entry price), e.g. "Close LONG SOL-PERP @ 145.20?". On
confirmation it `fetch()`s a POST to `/api/positions/{id}/close` and, on
success, `location.reload()`s so balance, positions, and trades all reflect
the close. On failure it shows an `alert()` with the error message. This
follows the existing inline vanilla-JS pattern used by `toggleReasoning()`
in the same template — no bundler, no framework, no new dependencies.

## Out of scope

No free-text exit reason, no confirmation modal beyond the native
`confirm()`, no optimistic UI update (a full reload is acceptable given the
low frequency of manual closes).
