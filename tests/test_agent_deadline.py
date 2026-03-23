"""Tests for BaseAgent deadline and gating context injection."""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from trellis.core.agent import BaseAgent


class StubAgent(BaseAgent):
    """Minimal concrete agent for testing."""
    def get_system_prompt(self, idea_id: str) -> str:
        return "You are a test agent."


@pytest.fixture
def agent():
    config = MagicMock()
    config.name = "ideation"
    config.model = "claude-haiku-4-5"
    config.max_turns = 30
    config.max_budget_usd = 1.0
    config.status = "active"
    blackboard = MagicMock()
    dispatcher = MagicMock()
    return StubAgent(config=config, blackboard=blackboard, dispatcher=dispatcher, project_root="/tmp")


def test_build_deadline_context(agent):
    """Deadline context includes time info in structured XML."""
    deadline = datetime.now(timezone.utc) + timedelta(minutes=25)
    ctx = agent._build_deadline_context(deadline)
    assert "<time-budget>" in ctx
    assert "25 minutes" in ctx or "24 minutes" in ctx  # allow 1min rounding
    assert "</time-budget>" in ctx


def test_build_deadline_context_zero_minutes(agent):
    """Deadline context handles zero remaining time."""
    deadline = datetime.now(timezone.utc)
    ctx = agent._build_deadline_context(deadline)
    assert "0 minutes" in ctx


def test_max_turns_override_respected(agent):
    """max_turns_override replaces config max_turns."""
    assert agent.config.max_turns == 30
    # The override is passed to _run_inner and used in ClaudeAgentOptions
    # We test this at the integration level — just verify the parameter exists
    import inspect
    sig = inspect.signature(agent.run)
    assert "max_turns_override" in sig.parameters
    assert "deadline" in sig.parameters


def test_llm_decides_context_injected(agent):
    """LLM-decides gating context is available for injection."""
    from trellis.core.agent import LLM_DECIDES_CONTEXT
    assert "<self-assessment>" in LLM_DECIDES_CONTEXT
    assert "needs_review" in LLM_DECIDES_CONTEXT
