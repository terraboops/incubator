"""Tests for the Handler abstraction (trellis/core/handlers)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from trellis.core.agent import AgentResult
from trellis.core.handlers import (
    AgentHandler,
    Handler,
    HandlerContext,
    HandlerResponse,
    create_handler,
)


def _make_context(**overrides) -> HandlerContext:
    base = dict(
        idea_id="idea-1",
        stage="prototype",
        role="ideation",
        blackboard_dir="/tmp/bb/idea-1",
        workspace_dir="/tmp/ws/idea-1",
        iteration=1,
    )
    base.update(overrides)
    return HandlerContext(**base)


class TestHandlerContext:
    def test_to_json_shape_matches_spec(self):
        ctx = _make_context(inputs={"version": "v1"}, previous_state="iterate")
        payload = ctx.to_json()
        assert payload == {
            "event": "role.service",
            "idea_id": "idea-1",
            "stage": "prototype",
            "role": "ideation",
            "iteration": 1,
            "blackboard_dir": "/tmp/bb/idea-1",
            "workspace_dir": "/tmp/ws/idea-1",
            "previous_state": "iterate",
            "inputs": {"version": "v1"},
        }

    def test_deadline_is_not_serialized_over_the_wire(self):
        # The handler contract is JSON-in; datetime deadlines are agent-internal.
        ctx = _make_context(deadline=datetime.now(timezone.utc))
        assert "deadline" not in ctx.to_json()


class TestHandlerResponse:
    def test_to_json_shape_matches_spec(self):
        resp = HandlerResponse(
            state="proceed",
            reason="done",
            artifacts={"deploy_url": "https://x"},
            cost_usd=1.23,
            metrics={"duration_ms": 4200},
        )
        assert resp.to_json() == {
            "state": "proceed",
            "reason": "done",
            "artifacts": {"deploy_url": "https://x"},
            "cost_usd": 1.23,
            "metrics": {"duration_ms": 4200},
        }

    def test_internal_fields_not_serialized(self):
        resp = HandlerResponse(
            state="proceed",
            session_id="sess-1",
            sandbox_failure=True,
            transcript=[{"role": "assistant"}],
        )
        payload = resp.to_json()
        assert "session_id" not in payload
        assert "sandbox_failure" not in payload
        assert "transcript" not in payload


class TestAgentHandler:
    @pytest.fixture
    def base_agent(self):
        agent = MagicMock()
        agent.run = AsyncMock()
        return agent

    async def test_success_maps_to_proceed(self, base_agent):
        base_agent.run.return_value = AgentResult(
            success=True,
            output="ok",
            cost_usd=0.42,
            session_id="sess-abc",
            stop_reason="end_turn",
            transcript=[{"role": "assistant"}],
        )
        handler = AgentHandler(base_agent)
        ctx = _make_context()

        resp = await handler.serve(ctx)

        assert resp.state == "proceed"
        assert resp.cost_usd == 0.42
        assert resp.session_id == "sess-abc"
        assert resp.metrics == {"stop_reason": "end_turn"}
        assert resp.transcript == [{"role": "assistant"}]
        assert resp.sandbox_failure is False

    async def test_failure_maps_to_iterate(self, base_agent):
        base_agent.run.return_value = AgentResult(
            success=False,
            error="connection reset",
            cost_usd=0.0,
        )
        handler = AgentHandler(base_agent)

        resp = await handler.serve(_make_context())

        assert resp.state == "iterate"
        assert resp.reason == "connection reset"

    async def test_sandbox_failure_maps_to_iterate(self, base_agent):
        base_agent.run.return_value = AgentResult(
            success=True,
            sandbox_failure=True,
            cost_usd=0.0,
            stop_reason="end_turn",
        )
        handler = AgentHandler(base_agent)

        resp = await handler.serve(_make_context())

        assert resp.state == "iterate"
        assert resp.sandbox_failure is True

    async def test_deadline_forwarded_to_agent_run(self, base_agent):
        base_agent.run.return_value = AgentResult(success=True)
        handler = AgentHandler(base_agent)
        deadline = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)

        await handler.serve(_make_context(deadline=deadline))

        base_agent.run.assert_awaited_once_with("idea-1", deadline=deadline)

    async def test_no_stop_reason_means_empty_metrics(self, base_agent):
        base_agent.run.return_value = AgentResult(success=True, stop_reason=None)
        handler = AgentHandler(base_agent)

        resp = await handler.serve(_make_context())

        assert resp.metrics == {}


class TestCreateHandlerFactory:
    def test_agent_type_returns_agent_handler(self):
        base_agent = MagicMock()
        handler = create_handler({"handler": "agent"}, agent=base_agent)
        assert isinstance(handler, AgentHandler)
        assert handler.agent is base_agent

    def test_missing_handler_key_defaults_to_agent(self):
        base_agent = MagicMock()
        handler = create_handler({"name": "ideation"}, agent=base_agent)
        assert isinstance(handler, AgentHandler)

    def test_agent_type_without_agent_kwarg_raises(self):
        with pytest.raises(ValueError, match="requires a BaseAgent"):
            create_handler({"handler": "agent"})

    def test_bogus_handler_type_raises(self):
        with pytest.raises(NotImplementedError, match="bogus"):
            create_handler({"handler": "bogus"})


class TestHandlerProtocol:
    def test_handler_is_abstract(self):
        with pytest.raises(TypeError):
            Handler()  # abstract; cannot instantiate

    def test_agent_handler_satisfies_protocol(self):
        agent = MagicMock()
        assert isinstance(AgentHandler(agent), Handler)
