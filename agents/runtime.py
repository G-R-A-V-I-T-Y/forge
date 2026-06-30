import logging
from pathlib import Path
from agents.decision_loop import run_decision

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        agent_id: str,
        thesis_path: str,
        config: dict,
        conn,
        get_market_fn,    # callable: (assets) -> dict
        llm_fn,           # callable: (sys_prompt, decision_prompt) -> dict
        bridge_factory,   # callable: (agent_id, conn, market_state) -> TradingBridge
    ):
        self.agent_id = agent_id
        self.thesis_path = Path(thesis_path)
        self.config = config
        self.conn = conn
        self.get_market_fn = get_market_fn
        self.llm_fn = llm_fn
        self.bridge_factory = bridge_factory

    def _load_thesis(self) -> str:
        """Returns thesis text, or 'No thesis loaded.' if file not found."""
        try:
            return self.thesis_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("[%s] Thesis file not found: %s", self.agent_id, self.thesis_path)
            return "No thesis loaded."

    async def tick(self) -> None:
        """Called by APScheduler on each wake interval. Never raises."""
        logger.info("[%s] Waking up", self.agent_id)
        try:
            thesis_text = self._load_thesis()
            result = run_decision(
                agent_id=self.agent_id,
                thesis_text=thesis_text,
                config=self.config,
                conn=self.conn,
                get_market_fn=self.get_market_fn,
                llm_fn=self.llm_fn,
                bridge_factory=self.bridge_factory,
            )
            logger.info("[%s] Decision: %s — %s",
                        self.agent_id, result["action"], result.get("detail", ""))
        except Exception as exc:
            logger.error("[%s] Unexpected tick error: %s", self.agent_id, exc, exc_info=True)
