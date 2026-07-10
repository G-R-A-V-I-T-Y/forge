"""agent_runner's llm_fn must accept agent_id and forward it to model_chain.decide()
so per-agent pinned models resolve, and so decision_loop's _call_llm_with_retry
(which always calls llm_fn(sp, dp, agent_id=agent_id)) doesn't TypeError.
See docs/STRATEGIC_ASSESSMENT_07_09_2026.md defect C1."""
from unittest.mock import patch

from agents.agent_runner import _build_llm_fn


def test_llm_fn_accepts_agent_id_kwarg():
    """Calling exactly as decision_loop._call_llm_with_retry does must not raise."""
    config = {"desk": {"starting_balance": 50000.0}}
    llm_fn = _build_llm_fn(config)

    with patch("agents.agent_runner.model_chain.decide") as mock_decide:
        mock_decide.return_value = ({"action": "wait", "reason": "test"}, "stub-model")
        result = llm_fn("system prompt text", "decision prompt text", agent_id="jade_hawk")

    assert result == ({"action": "wait", "reason": "test"}, "stub-model")


def test_pinned_model_forwarded():
    """agent_id must reach model_chain.decide() unchanged so its pinned-model
    lookup (llm/model_chain.py's decide()) actually runs for this agent."""
    config = {"desk": {"starting_balance": 50000.0}}
    llm_fn = _build_llm_fn(config)

    with patch("agents.agent_runner.model_chain.decide") as mock_decide:
        mock_decide.return_value = ({"action": "wait", "reason": "test"}, "pinned-model")
        llm_fn("sp", "dp", agent_id="silver_basin")

    mock_decide.assert_called_once_with(
        "sp", "dp", config=config, agent_id="silver_basin"
    )


def test_llm_fn_default_agent_id_is_none():
    """agent_id must be optional (decision_loop's retry path is the only
    production caller that always supplies it, but the parameter itself
    must default sanely if called without it)."""
    config = {"desk": {"starting_balance": 50000.0}}
    llm_fn = _build_llm_fn(config)

    with patch("agents.agent_runner.model_chain.decide") as mock_decide:
        mock_decide.return_value = ({"action": "wait"}, None)
        llm_fn("sp", "dp")

    mock_decide.assert_called_once_with("sp", "dp", config=config, agent_id=None)
