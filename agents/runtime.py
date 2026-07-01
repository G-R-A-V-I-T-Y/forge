import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

from agents.decision_loop import run_decision

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        agent_id: str,
        thesis_path: str,
        config: dict,
        conn,
        provider,
        llm_fn,
        bridge_factory,
        scheduler=None,
    ):
        self.agent_id = agent_id
        self.thesis_path = Path(thesis_path)
        self.config = config
        self.conn = conn
        self.provider = provider
        self.llm_fn = llm_fn
        self.bridge_factory = bridge_factory
        self.scheduler = scheduler
        self._last_wake_interval = config.get("desk", {}).get("wake_interval_seconds")

    def _load_thesis(self) -> str:
        try:
            return self.thesis_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("[%s] Thesis file not found: %s", self.agent_id, self.thesis_path)
            return "No thesis loaded."

    def _read_agent_config(self) -> dict:
        """Read per-agent config_json from SQLite each tick (not cached)."""
        row = self.conn.execute(
            "SELECT config_json FROM agents WHERE id = ?", (self.agent_id,)
        ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _reschedule_if_interval_changed(self, new_interval: int) -> None:
        """Reschedule the APScheduler job if the wake interval changed."""
        if self.scheduler is None:
            return
        if self._last_wake_interval == new_interval:
            return
        self._last_wake_interval = new_interval
        try:
            self.scheduler.reschedule_job(
                self.agent_id,
                trigger="interval",
                seconds=new_interval,
            )
            logger.info(
                "[%s] Wake interval updated to %ds",
                self.agent_id, new_interval,
            )
        except Exception as exc:
            logger.warning(
                "[%s] Could not reschedule interval: %s", self.agent_id, exc,
            )

    async def tick(self) -> None:
        logger.info("[%s] Waking up", self.agent_id)
        try:
            # Read per-agent config from SQLite each cycle
            agent_overrides = self._read_agent_config()
            wake_interval = agent_overrides.get(
                "wake_interval",
                self.config["desk"]["wake_interval_seconds"],
            )
            self._reschedule_if_interval_changed(wake_interval)

            thesis_text = self._load_thesis()
            result = await run_decision(
                agent_id=self.agent_id,
                thesis_text=thesis_text,
                config=self.config,
                conn=self.conn,
                provider=self.provider,
                llm_fn=self.llm_fn,
                bridge_factory=self.bridge_factory,
            )
            logger.info("[%s] Decision: %s — %s",
                        self.agent_id, result["action"], result.get("detail", ""))
        except Exception as exc:
            logger.error("[%s] Unexpected tick error: %s", self.agent_id, exc, exc_info=True)
