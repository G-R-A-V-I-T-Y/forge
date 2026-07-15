"""meta/risk_officer.py — Central risk oversight for all agents.

Enforces per-agent and desk-wide risk limits:
  - Total drawdown kill switch (configurable % of desk equity)
  - Per-agent max position size
  - Daily loss limits
  - Suspicious activity monitoring (single-agent concentration)
  - Aggregate gross-exposure throttle vs desk equity (prorated entry-disable)
  - Event-calendar blackout windows (no new entries pre-FOMC/CPI)
  - Regime memo (market state snapshot for Head of Desk / web layer)
  - Entry-gate disablement

Runs on a 30-60 min cadence (see forge.py's "risk_officer" job).

Every mutation this module makes flows through `apply_actions()` ->
`validate_risk_actions()` — a separate, dumb, default-deny gate that
asserts the risk officer can only ever hold or reduce risk, never add it.
See the "Reduce-Only Action Validator" section below.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Pre-event blackout window (criterion c): no new entries within this many
# hours before a calendar event (FOMC/CPI/etc).
EVENT_BLACKOUT_HOURS = 2

# Settings-table key the latest regime memo is persisted under (criterion a).
# Uses store/settings.py's existing generic key/value machinery — no new
# table needed; only the latest memo is required by downstream readers.
REGIME_MEMO_SETTINGS_KEY = "risk_officer_latest_regime_memo"

# Reason prefix identifying entry_disables rows authored by the
# gross-exposure throttle. _reconcile_throttle uses it to recognise its own
# rows across cycles (idempotent disable, restore-when-under-threshold).
THROTTLE_REASON_PREFIX = "gross exposure throttle"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


# ===========================================================================
# Reduce-Only Action Validator
# ===========================================================================
#
# The one hard invariant of this module: everything the risk officer does
# must strictly reduce or hold risk flat. This validator is deliberately
# dumb and separate from RiskOfficer's decision logic — it knows nothing
# about *why* an action was proposed, only whether its shape could ever
# increase risk. ALL risk officer mutations (including the pre-existing
# kill-switch / concentration / entry-gate paths) flow through
# RiskOfficer.apply_actions() -> validate_risk_actions() before they touch
# the database. Unknown action types are rejected by default.

# The only action shapes the risk officer is ever allowed to emit.
ALLOWED_RISK_ACTION_TYPES = {"disable_entry", "enable_entry", "memo"}

# Any of these keys appearing on ANY action — regardless of its declared
# type — is an automatic reject. The risk officer never legitimately needs
# to carry a size/leverage/stop/entry field; a well-typed action that
# smuggles one in is exactly the "buggy rule" this gate exists to catch.
RISK_INCREASING_FIELDS = {
    "size_pct", "position_size_pct", "size", "notional_usd", "true_notional",
    "leverage", "stop_loss_price", "take_profit_price", "sl", "tp",
    "stop_loss", "take_profit", "entry_price", "asset", "direction",
}


class RiskActionRejected(Exception):
    """Raised by validate_risk_actions when a proposed action could
    increase risk, or does not match a known, vetted action shape. A
    rejection here means a bug in the risk officer's own decision logic —
    it must surface loudly, never be swallowed."""


def validate_risk_actions(actions: list[dict[str, Any]], conn) -> list[dict[str, Any]]:
    """The reduce-only gate every risk-officer action list must pass
    through before being applied.

    Raises RiskActionRejected on the first violation. Returns the same
    list unchanged if every action is clean.
    """
    for action in actions:
        if not isinstance(action, dict):
            raise RiskActionRejected(f"action is not a dict: {action!r}")

        action_type = action.get("type")
        if action_type not in ALLOWED_RISK_ACTION_TYPES:
            raise RiskActionRejected(
                f"unknown/disallowed risk officer action type: {action_type!r} "
                f"(allowed: {sorted(ALLOWED_RISK_ACTION_TYPES)})"
            )

        smuggled = RISK_INCREASING_FIELDS & action.keys()
        if smuggled:
            raise RiskActionRejected(
                f"action of type {action_type!r} carries risk-increasing "
                f"field(s) {sorted(smuggled)} — risk officer actions must "
                f"never touch size/leverage/stop/entry fields: {action!r}"
            )

        if action_type == "disable_entry":
            if not action.get("agent_id"):
                raise RiskActionRejected(f"disable_entry missing agent_id: {action!r}")

        elif action_type == "enable_entry":
            agent_id = action.get("agent_id")
            if not agent_id:
                raise RiskActionRejected(f"enable_entry missing agent_id: {action!r}")
            # Restore-to-baseline only: the officer may re-enable an entry
            # gate it closed itself (exposure fell back under threshold),
            # but must never touch a human-set disable, and must never
            # "enable" an agent it never disabled in the first place.
            row = conn.execute(
                """SELECT 1 FROM entry_disables
                   WHERE agent_id = ? AND enabled_at IS NULL AND disabled_by = 'risk_officer'
                   LIMIT 1""",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise RiskActionRejected(
                    f"enable_entry for {agent_id!r} does not correspond to an "
                    f"open risk-officer disable — the officer may only restore "
                    f"its own throttle, never a human-set or nonexistent disable"
                )

        elif action_type == "memo":
            if "content" not in action:
                raise RiskActionRejected(f"memo action missing content: {action!r}")

    return actions


class RiskOfficer:
    """Central risk supervisor. Loads desk config once per cycle check."""

    def __init__(self, conn, config: dict | None = None):
        self.conn = conn
        self.config = config or _load_config()

    # ── Reduce-Only Apply Path ─────────────────────────────────────
    # Single choke point for every DB mutation this class makes. Anything
    # that wants to disable/enable an entry gate or persist a memo must go
    # through here, so nothing can bypass validate_risk_actions().

    def apply_actions(self, actions: list[dict[str, Any]]) -> None:
        validate_risk_actions(actions, self.conn)
        for action in actions:
            action_type = action["type"]
            if action_type == "disable_entry":
                self._apply_disable_entry(
                    action["agent_id"], action["reason"],
                    action.get("disabled_by", "risk_officer"),
                )
            elif action_type == "enable_entry":
                self._apply_enable_entry(action["agent_id"])
            elif action_type == "memo":
                self._apply_memo(action["content"])

    def _apply_disable_entry(self, agent_id: str, reason: str, disabled_by: str) -> None:
        self.conn.execute(
            """INSERT INTO entry_disables
                   (agent_id, disabled_by, disabled_at, reason)
               VALUES (?, ?, ?, ?)""",
            (agent_id, disabled_by, _now(), reason),
        )
        self.conn.commit()
        logger.info("Entry disabled for %s: %s", agent_id, reason)

    def _apply_enable_entry(self, agent_id: str) -> None:
        self.conn.execute(
            """UPDATE entry_disables SET enabled_at = ?
               WHERE agent_id = ? AND enabled_at IS NULL AND disabled_by = 'risk_officer'""",
            (_now(), agent_id),
        )
        self.conn.commit()
        logger.info("Entry re-enabled for %s", agent_id)

    def _apply_memo(self, memo: dict[str, Any]) -> None:
        from store import settings as settings_store
        settings_store.set_value(self.conn, REGIME_MEMO_SETTINGS_KEY, memo)

    # ── Desk-Level Checks ──────────────────────────────────────────

    def desk_in_kill_switch(self) -> bool:
        """Check whether total desk drawdown triggers the kill switch.

        Kill switch fires when the aggregate paper P&L drops below
        drawdown_kill_pct from peak equity.
        """
        kill_pct = float(self.config.get("drawdown_kill_pct", 25)) / 100.0

        total_balance = self.conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE mode = 'paper'"
        ).fetchone()[0]
        total_peak = self.conn.execute(
            "SELECT COALESCE(SUM(peak_balance), 0) FROM accounts WHERE mode = 'paper'"
        ).fetchone()[0]

        if total_peak <= 0:
            return False

        drawdown = (total_peak - total_balance) / total_peak
        if drawdown >= kill_pct:
            logger.warning(
                "DESK KILL SWITCH: drawdown %.1f%% >= %.1f%%",
                drawdown * 100, kill_pct * 100,
            )
            return True
        return False

    def desk_daily_loss_exceeded(self) -> bool:
        """Check if total desk daily loss exceeds the configured limit."""
        daily_loss_limit = float(self.config.get("daily_loss_limit", 500))

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = self.conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) FROM trades
               WHERE status = 'closed' AND voided = 0
               AND DATE(entry_timestamp) = ?""",
            (today,),
        ).fetchone()[0]

        if daily_pnl < -daily_loss_limit:
            logger.warning(
                "DESK DAILY LOSS: %.2f exceeds limit %.2f",
                daily_pnl, -daily_loss_limit,
            )
            return True
        return False

    def agent_concentration_exceeded(self, threshold: float = 0.40) -> list[str]:
        total_positions = self.conn.execute(
            "SELECT COALESCE(SUM(notional_usd), 0) FROM positions"
        ).fetchone()[0]

        if total_positions <= 0:
            return []

        rows = self.conn.execute(
            """SELECT agent_id, COALESCE(SUM(notional_usd), 0) as total
               FROM positions GROUP BY agent_id"""
        ).fetchall()

        violators = []
        for row in rows:
            share = row["total"] / total_positions if total_positions > 0 else 0
            if share > threshold:
                violators.append(row["agent_id"])
        return violators

    def gross_exposure_throttle(self) -> list[str]:
        """Criterion (b): aggregate gross notional across ALL agents' open
        positions vs desk.max_gross_exposure_mult x total desk equity.

        Returns the list of agent_ids to entry-disable, highest-exposure
        first, working down only as far as needed to bring the remaining
        (still-enabled) agents' aggregate exposure back under the
        threshold. Existing positions are never touched — this only ever
        withholds *new* entries via the entry-disable path.
        """
        desk_cfg = self.config["desk"]
        max_mult = float(desk_cfg["max_gross_exposure_mult"])

        total_equity = self.conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE mode = 'paper'"
        ).fetchone()[0]
        if total_equity <= 0:
            return []
        threshold = max_mult * total_equity

        rows = self.conn.execute(
            """SELECT agent_id, COALESCE(SUM(notional_usd), 0) as total
               FROM positions GROUP BY agent_id"""
        ).fetchall()
        exposures = {row["agent_id"]: row["total"] for row in rows}
        remaining_total = sum(exposures.values())
        if remaining_total <= threshold:
            return []

        ordered = sorted(exposures.items(), key=lambda kv: kv[1], reverse=True)
        to_disable: list[str] = []
        for agent_id, notional in ordered:
            if remaining_total <= threshold:
                break
            to_disable.append(agent_id)
            remaining_total -= notional
        return to_disable

    def _reconcile_throttle(self, throttled: list[str], desk_cfg: dict[str, Any]) -> None:
        """Idempotently sync entry_disables with the current throttle set:
        disable newly-throttled agents (once — repeated cycles over the
        limit must not stack duplicate rows), and restore agents the
        throttle no longer needs once exposure falls back under the
        threshold. Restoring is safe by construction: enable_entry can
        only lift the officer's own disables (validator-enforced), never
        a human-set one.
        """
        open_rows = self.conn.execute(
            """SELECT DISTINCT agent_id FROM entry_disables
               WHERE enabled_at IS NULL AND disabled_by = 'risk_officer'
                 AND reason LIKE ?""",
            (THROTTLE_REASON_PREFIX + "%",),
        ).fetchall()
        currently_disabled = {row["agent_id"] for row in open_rows}

        for agent_id in throttled:
            if agent_id not in currently_disabled:
                self.disable_entry(
                    agent_id,
                    f"{THROTTLE_REASON_PREFIX}: desk exposure exceeds "
                    f"{desk_cfg['max_gross_exposure_mult']}x equity",
                )

        for agent_id in sorted(currently_disabled - set(throttled)):
            self.enable_entry(agent_id)

    def event_blackout_active(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Criterion (c): desk-wide blackout within EVENT_BLACKOUT_HOURS
        before any desk.event_calendar entry. Returns the triggering event
        dict, or None if no blackout is active.

        This is a pure, dynamically-computed gate (same shape as
        desk_in_kill_switch) — it operates purely through
        is_entry_gate_open, never persists a row, and therefore clears
        itself automatically the instant `now` passes the event time.
        Existing positions are never touched.

        Malformed calendar entries fail loudly. An absent/empty list is
        valid (no blackouts).
        """
        desk_cfg = self.config["desk"]
        calendar = desk_cfg.get("event_calendar") or []
        if not isinstance(calendar, list):
            raise ValueError(
                f"desk.event_calendar must be a list, got {type(calendar).__name__}"
            )

        now = now or datetime.now(timezone.utc)
        for entry in calendar:
            if not isinstance(entry, dict) or "name" not in entry or "at" not in entry:
                raise ValueError(f"malformed desk.event_calendar entry: {entry!r}")
            try:
                at = datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
            except ValueError as e:
                raise ValueError(
                    f"malformed desk.event_calendar entry 'at' value: {entry!r}"
                ) from e
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)

            window_start = at - timedelta(hours=EVENT_BLACKOUT_HOURS)
            if window_start <= now < at:
                return entry
        return None

    # ── Per-Agent Checks ──────────────────────────────────────────

    def agent_position_limit_exceeded(self, agent_id: str) -> bool:
        max_pos = float(self.config.get("max_position_size", 1000))
        current_size = self.conn.execute(
            """SELECT COALESCE(SUM(notional_usd), 0) FROM positions
               WHERE agent_id = ?""",
            (agent_id,),
        ).fetchone()[0]
        return current_size > max_pos

    def agent_daily_loss_exceeded(self, agent_id: str) -> bool:
        """Check if agent exceeded its daily loss limit."""
        daily_limit = float(self.config.get("agent_daily_loss", 100))

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = self.conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) FROM trades
               WHERE agent_id = ? AND status = 'closed' AND voided = 0
               AND DATE(entry_timestamp) = ?""",
            (agent_id, today),
        ).fetchone()[0]

        return pnl < -daily_limit

    def agent_is_killed(self, agent_id: str) -> bool:
        """Criterion (d): per-agent kill flag.

        `agents.status == 'suspended'` already IS this flag — it is
        persistent (survives across cycles, unlike the per-cycle
        entry_disables throttle), distinct from the gross-exposure/
        concentration throttle, and already settable via existing
        machinery: meta/controller.py (evaluator lifecycle decisions),
        web/app.py's human "stop agent" action, and read by
        meta/evaluator.py and this class's own run_cycle roster query.
        No new mechanism is introduced; this just gives RiskOfficer
        callers a single named entry point onto the existing flag.
        """
        row = self.conn.execute(
            "SELECT status FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        return bool(row) and row["status"] == "suspended"

    # ── Regime Memo ─────────────────────────────────────────────────

    def build_regime_memo(self) -> dict[str, Any] | None:
        """Criterion (a): a short structured memo from current market
        state. Reuses market/heartbeat.py's existing regime computation
        (_compute_regime, classify_regime) via the heartbeat packet — no
        new market-state machinery. Returns None (never raises) if no
        fresh heartbeat is available; memo generation must never block
        the risk cycle.
        """
        from market.heartbeat import (
            DEFAULT_HEARTBEAT_PATH,
            heartbeat_max_age_seconds,
            read_heartbeat_or_none,
        )

        desk_cfg = self.config.get("desk") or {}
        heartbeat_path = desk_cfg.get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
        try:
            packet = read_heartbeat_or_none(
                heartbeat_path, heartbeat_max_age_seconds(self.config)
            )
        except Exception:
            logger.warning("risk officer: heartbeat read failed for regime memo", exc_info=True)
            return None
        if not packet:
            return None

        regime = packet.get("regime") or {}
        return {
            "generated_at": _now(),
            "heartbeat_timestamp": packet.get("timestamp"),
            "regime_tag": regime.get("regime_tag"),
            "average_volatility": regime.get("average_volatility"),
            "average_funding": regime.get("average_funding"),
            "risk_on_score": regime.get("risk_on_score"),
            "trend_score": regime.get("trend_score"),
            "crypto_fear_index": regime.get("crypto_fear_index"),
            "btc_dominance": regime.get("btc_dominance"),
        }

    def persist_regime_memo(self, memo: dict[str, Any]) -> None:
        self.apply_actions([{"type": "memo", "content": memo}])

    @staticmethod
    def latest_regime_memo(conn) -> dict[str, Any] | None:
        """Read the latest persisted regime memo (Head of Desk / web layer
        entry point)."""
        from store import settings as settings_store
        return settings_store.get(conn, REGIME_MEMO_SETTINGS_KEY)

    # ── Entry-Gate Management ─────────────────────────────────────

    def is_entry_gate_open(self, agent_id: str, now: datetime | None = None) -> bool:
        """Check if the entry gate is open for this agent.

        An entry can be disabled (gate closed) by:
          - Desk kill switch active
          - Event-calendar blackout window active (desk-wide)
          - Agent-specific disable (via entry_disables table)
          - Individual risk rule violation
        """
        if self.desk_in_kill_switch():
            return False

        if self.event_blackout_active(now) is not None:
            return False

        disabled = self.conn.execute(
            """SELECT 1 FROM entry_disables
               WHERE agent_id = ? AND enabled_at IS NULL
               LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if disabled:
            return False

        if self.agent_position_limit_exceeded(agent_id):
            return False

        if self.agent_daily_loss_exceeded(agent_id):
            return False

        return True

    def disable_entry(self, agent_id: str, reason: str, disabled_by: str = "risk_officer") -> None:
        """Disable the entry gate for an agent."""
        self.apply_actions([{
            "type": "disable_entry", "agent_id": agent_id,
            "reason": reason, "disabled_by": disabled_by,
        }])

    def enable_entry(self, agent_id: str) -> None:
        """Re-enable the entry gate for an agent (restore-to-baseline only:
        this can only lift a disable the risk officer itself created —
        see validate_risk_actions)."""
        self.apply_actions([{"type": "enable_entry", "agent_id": agent_id}])

    # ── Full Risk Check ──────────────────────────────────────────

    def run_cycle(self) -> dict[str, Any]:
        """Run a full risk-check cycle across all agents.

        Returns a report dict with desk and per-agent findings.
        """
        report = {
            "checked_at": _now(),
            "desk_kill_switch": self.desk_in_kill_switch(),
            "desk_daily_loss_exceeded": self.desk_daily_loss_exceeded(),
            "concentration_violators": self.agent_concentration_exceeded(),
            "gross_exposure_throttled_agents": [],
            "event_blackout": None,
            "regime_memo": None,
            "agents": {},
        }

        # config["desk"] is required — per the repo config convention a
        # missing desk block must fail loudly here, never silently skip
        # the throttle/blackout/memo protections.
        desk_cfg = self.config["desk"]

        report["event_blackout"] = self.event_blackout_active()

        throttled = self.gross_exposure_throttle()
        report["gross_exposure_throttled_agents"] = throttled
        self._reconcile_throttle(throttled, desk_cfg)

        memo = self.build_regime_memo()
        if memo is not None:
            self.persist_regime_memo(memo)
            report["regime_memo"] = memo

        # Check each active/rookie agent
        rows = self.conn.execute(
            """SELECT id, status FROM agents
               WHERE status IN ('rookie', 'active', 'suspended')
               ORDER BY name"""
        ).fetchall()

        for row in rows:
            agent_id = row["id"]
            status = row["status"]
            agent_report = {
                "status": status,
                "position_limit_exceeded": self.agent_position_limit_exceeded(agent_id),
                "daily_loss_exceeded": self.agent_daily_loss_exceeded(agent_id),
                "entry_gate_open": self.is_entry_gate_open(agent_id),
            }

            # NOTE: a closed gate is deliberately NOT materialized into an
            # entry_disables row here. is_entry_gate_open() is the live
            # composite check (kill switch, blackout, disables, per-agent
            # limits) consumed at decision time; persisting its transient
            # components (a 2h blackout, a daily-loss day) would outlive
            # the condition and freeze agents permanently. Only the
            # gross-exposure throttle persists rows, and it reconciles
            # them each cycle (_reconcile_throttle).

            report["agents"][agent_id] = agent_report

        return report


def risk_check_cycle(conn, config: dict | None = None) -> dict[str, Any]:
    """Convenience function: instantiate RiskOfficer and run one cycle."""
    officer = RiskOfficer(conn, config)
    return officer.run_cycle()


def apply_risk_verdict(
    result: dict[str, Any],
) -> bool:
    """Return True if the desk is cleared for trading based on a risk cycle result."""
    if result.get("desk_kill_switch"):
        return False
    if result.get("desk_daily_loss_exceeded"):
        return False
    return True
