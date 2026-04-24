"""Tests for handler retry + timeout behaviour."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from trellis.core.handlers import (
    AgentHandler,
    Handler,
    HandlerContext,
    HandlerResponse,
    RetryPolicy,
    create_handler,
    parse_duration,
    with_retry,
)


def _ctx(role: str = "r", stage: str = "s") -> HandlerContext:
    return HandlerContext(
        idea_id="i",
        stage=stage,
        role=role,
        blackboard_dir="/bb",
        workspace_dir="/ws",
    )


class _ScriptedHandler(Handler):
    """Test double: yields a scripted sequence of responses or raises."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def serve(self, context):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _SleepyHandler(Handler):
    """Test double: sleeps forever until cancelled."""

    async def serve(self, context):
        await asyncio.sleep(3600)
        return HandlerResponse(state="proceed")


class TestParseDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            (0, None),
            (30, 30.0),
            (30.5, 30.5),
            ("30", 30.0),
            ("30s", 30.0),
            ("30sec", 30.0),
            ("10m", 600.0),
            ("10min", 600.0),
            ("2h", 7200.0),
            ("", None),
            ("garbage", None),
        ],
    )
    def test_variants(self, value, expected):
        assert parse_duration(value) == expected


class TestRetryPolicy:
    def test_default_is_inactive(self):
        p = RetryPolicy()
        assert not p.is_active()

    def test_max_attempts_makes_active(self):
        assert RetryPolicy(max_attempts=3).is_active()

    def test_timeout_makes_active(self):
        assert RetryPolicy(timeout_sec=30).is_active()

    def test_exponential_backoff_ladder(self):
        p = RetryPolicy(
            max_attempts=10,
            backoff="exponential",
            initial_delay_sec=30,
            max_delay_sec=300,
        )
        # Per spec: 30 → 60 → 120 → 240 → 300 (cap)
        # attempt 0 = first try (no wait)
        assert p.backoff_for(0) == 0
        assert p.backoff_for(1) == 30
        assert p.backoff_for(2) == 60
        assert p.backoff_for(3) == 120
        assert p.backoff_for(4) == 240
        assert p.backoff_for(5) == 300  # capped
        assert p.backoff_for(6) == 300

    def test_linear_backoff(self):
        p = RetryPolicy(backoff="linear", initial_delay_sec=10, max_delay_sec=60)
        assert p.backoff_for(1) == 10
        assert p.backoff_for(2) == 20
        assert p.backoff_for(7) == 60  # capped

    def test_fixed_backoff(self):
        p = RetryPolicy(backoff="fixed", initial_delay_sec=15)
        assert p.backoff_for(1) == 15
        assert p.backoff_for(5) == 15

    def test_from_role_config_parses_spec_shape(self):
        cfg = {
            "name": "deploy",
            "handler": "script",
            "timeout": "10m",
            "retry": {
                "max_attempts": 5,
                "backoff": "exponential",
                "on_timeout": "retry",
                "terminal_after": "failed",
            },
        }
        p = RetryPolicy.from_role_config(cfg)
        assert p.timeout_sec == 600
        assert p.max_attempts == 5
        assert p.backoff == "exponential"
        assert p.on_timeout == "retry"
        assert p.terminal_state == "failed"

    def test_from_role_config_empty(self):
        p = RetryPolicy.from_role_config({})
        assert not p.is_active()


class TestWithRetry:
    async def test_success_on_first_attempt_no_retry(self):
        inner = _ScriptedHandler([HandlerResponse(state="proceed")])
        wrapped = with_retry(inner, RetryPolicy(max_attempts=3, initial_delay_sec=0))

        resp = await wrapped.serve(_ctx())

        assert resp.state == "proceed"
        assert inner.calls == 1

    async def test_retries_on_iterate_until_success(self):
        inner = _ScriptedHandler(
            [
                HandlerResponse(state="iterate", reason="not yet"),
                HandlerResponse(state="iterate", reason="still not"),
                HandlerResponse(state="proceed", reason="ok now"),
            ]
        )
        wrapped = with_retry(inner, RetryPolicy(max_attempts=5, initial_delay_sec=0))

        resp = await wrapped.serve(_ctx())

        assert resp.state == "proceed"
        assert inner.calls == 3

    async def test_exhausts_attempts_returns_terminal_state(self):
        inner = _ScriptedHandler([HandlerResponse(state="iterate", reason="nope")] * 3)
        wrapped = with_retry(
            inner, RetryPolicy(max_attempts=3, initial_delay_sec=0, terminal_state="failed")
        )

        resp = await wrapped.serve(_ctx())

        assert resp.state == "failed"
        assert "exhausted 3 attempts" in resp.reason
        assert "nope" in resp.reason
        assert inner.calls == 3

    async def test_handler_exception_counts_as_iterate(self):
        inner = _ScriptedHandler(
            [
                RuntimeError("transient"),
                HandlerResponse(state="proceed"),
            ]
        )
        wrapped = with_retry(inner, RetryPolicy(max_attempts=3, initial_delay_sec=0))

        resp = await wrapped.serve(_ctx())

        assert resp.state == "proceed"
        assert inner.calls == 2

    async def test_needs_review_short_circuits_retry(self):
        inner = _ScriptedHandler(
            [
                HandlerResponse(state="needs_review", reason="escalate"),
                HandlerResponse(state="proceed"),
            ]
        )
        wrapped = with_retry(inner, RetryPolicy(max_attempts=5, initial_delay_sec=0))

        resp = await wrapped.serve(_ctx())

        assert resp.state == "needs_review"
        assert inner.calls == 1

    async def test_timeout_becomes_iterate_and_retries(self):
        inner = _SleepyHandler()
        wrapped = with_retry(
            inner,
            RetryPolicy(
                max_attempts=2,
                timeout_sec=0.05,
                initial_delay_sec=0,
                on_timeout="retry",
            ),
        )

        resp = await wrapped.serve(_ctx())

        assert resp.state == "failed"  # both attempts timed out
        assert "Timed out" in resp.reason

    async def test_timeout_with_on_timeout_fail_terminates_immediately(self):
        inner = _SleepyHandler()
        wrapped = with_retry(
            inner,
            RetryPolicy(
                max_attempts=5,
                timeout_sec=0.05,
                initial_delay_sec=0,
                on_timeout="fail",
            ),
        )

        resp = await wrapped.serve(_ctx())

        assert resp.state == "failed"
        assert "on_timeout=fail" in resp.reason

    async def test_inactive_policy_does_not_wrap(self):
        inner = _ScriptedHandler([HandlerResponse(state="iterate")])
        result = with_retry(inner, RetryPolicy())  # max_attempts=1, no timeout
        # Should return the same object, not a wrapper.
        assert result is inner

    async def test_backoff_delay_applied_between_attempts(self):
        inner = _ScriptedHandler(
            [
                HandlerResponse(state="iterate"),
                HandlerResponse(state="proceed"),
            ]
        )
        policy = RetryPolicy(max_attempts=3, backoff="fixed", initial_delay_sec=0.05)
        wrapped = with_retry(inner, policy)

        start = asyncio.get_event_loop().time()
        await wrapped.serve(_ctx())
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed >= 0.04  # fixed 50ms delay between attempts


class TestCreateHandlerAppliesRetry:
    def test_retry_config_wraps_agent_handler(self):
        role_config = {
            "handler": "agent",
            "timeout": "10m",
            "retry": {"max_attempts": 3},
        }
        base_agent = MagicMock()
        handler = create_handler(role_config, agent=base_agent)

        # Should be wrapped, not a bare AgentHandler
        assert not isinstance(handler, AgentHandler)
        # Inner handler should still be an AgentHandler
        assert isinstance(handler.inner, AgentHandler)
        assert handler.policy.max_attempts == 3
        assert handler.policy.timeout_sec == 600

    def test_no_retry_config_returns_bare_handler(self):
        handler = create_handler({"handler": "agent"}, agent=MagicMock())
        assert isinstance(handler, AgentHandler)
