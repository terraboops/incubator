"""Tests for PoolManager result handling and release cap logic."""

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call

import pytest

from trellis.core.registry import AgentConfig, Registry
from trellis.orchestrator.job_queue import JobQueue
from trellis.orchestrator.pool import PoolManager
from trellis.orchestrator.worker import RunResult, RunStatus


def _make_pool_for_handle_result(tmp_path):
    """Build a minimal PoolManager suitable for _handle_result tests."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(
        pool_size=2, job_timeout_minutes=60,
        producer_interval_seconds=10, project_root=tmp_path,
        telegram_bot_token="test", telegram_chat_id="test",
    )
    pm.blackboard = MagicMock()
    pm.lock_manager = MagicMock()
    pm.roles = ["ideation", "implementation", "validation", "release"]
    # Pipeline agents: no cadence, no phase="*"
    def _get_agent(name):
        m = MagicMock()
        m.cadence = None
        m.phase = name
        m.max_concurrent = 1
        return m
    pm.registry = MagicMock()
    pm.registry.get_agent.side_effect = _get_agent
    pm.pool_dir = tmp_path / "pool"
    pm.pool_dir.mkdir(exist_ok=True)
    pm.workers = []
    return pm


def _make_release_result(idea_id: str = "test-idea") -> RunResult:
    """RunResult representing a successful release stage completion."""
    return RunResult(
        status=RunStatus.OK,
        role="release",
        idea_id=idea_id,
        duration_seconds=10.0,
        cost_usd=0.05,
    )


def _status_with_history(prior_release_count: int, max_cycles: int | None = None) -> dict:
    """Build a status dict with N prior 'released' phase_history entries."""
    history = [
        {"from": "release", "to": "released", "at": "2026-03-13T00:00:00Z"}
        for _ in range(prior_release_count)
    ]
    status: dict = {
        "id": "test-idea",
        "phase": "release",
        "phase_history": history,
        "last_serviced_by": {},
        "total_cost_usd": 0.0,
        "iteration_count": 0,
        "stage_results": {},
        "phase_recommendation": "proceed",
        "deadline_hits": {},
    }
    if max_cycles is not None:
        status["max_refinement_cycles"] = max_cycles
    return status


@pytest.mark.asyncio
async def test_release_cap_terminates_at_cap(tmp_path):
    """When prior_releases >= max_refinement_cycles, phase is set to terminal 'released'."""
    pm = _make_pool_for_handle_result(tmp_path)
    queue = JobQueue()

    # 1 prior release, default cap = 1 → at cap → must terminate
    status = _status_with_history(prior_release_count=1)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = _make_release_result()

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    # Must have set phase="released" (terminal), NOT phase="submitted"
    calls = pm.blackboard.update_status.call_args_list
    final_phase_calls = [c for c in calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert final_phase_calls, "update_status with a phase kwarg should have been called"
    last_phase_call = final_phase_calls[-1]
    assert last_phase_call.kwargs["phase"] == "released", (
        f"Expected terminal 'released', got {last_phase_call.kwargs['phase']}"
    )
    # stage_results should NOT be cleared (that only happens on loop-back)
    assert "stage_results" not in last_phase_call.kwargs


@pytest.mark.asyncio
async def test_release_cap_loops_back_under_cap(tmp_path):
    """When prior_releases < max_refinement_cycles, phase is reset to 'submitted' for refinement."""
    pm = _make_pool_for_handle_result(tmp_path)
    queue = JobQueue()

    # 0 prior releases, default cap = 1 → under cap → loop back
    status = _status_with_history(prior_release_count=0)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = _make_release_result()

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls = pm.blackboard.update_status.call_args_list
    final_phase_calls = [c for c in calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert final_phase_calls, "update_status with a phase kwarg should have been called"
    last_phase_call = final_phase_calls[-1]
    assert last_phase_call.kwargs["phase"] == "submitted", (
        f"Expected loop-back 'submitted', got {last_phase_call.kwargs['phase']}"
    )
    # stage_results must be cleared on loop-back
    assert last_phase_call.kwargs.get("stage_results") == {}


@pytest.mark.asyncio
async def test_release_cap_default_is_one_cycle(tmp_path):
    """Default max_refinement_cycles=1: first run loops, second terminates."""
    pm = _make_pool_for_handle_result(tmp_path)
    queue = JobQueue()

    for prior_releases, expected_phase in [(0, "submitted"), (1, "released")]:
        pm.blackboard.reset_mock()
        status = _status_with_history(prior_release_count=prior_releases)
        pm.blackboard.get_status.return_value = status
        pm.blackboard.next_agent.return_value = None
        pm.blackboard.is_ready.return_value = True
        pm.blackboard.get_gating_mode.return_value = "auto"

        result = _make_release_result()

        with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
            await pm._handle_result(result, queue)

        calls = pm.blackboard.update_status.call_args_list
        final_phase_calls = [c for c in calls if c.kwargs.get("phase") in ("released", "submitted")]
        assert final_phase_calls, f"No phase-setting call found for prior_releases={prior_releases}"
        assert final_phase_calls[-1].kwargs["phase"] == expected_phase, (
            f"prior_releases={prior_releases}: expected '{expected_phase}', "
            f"got '{final_phase_calls[-1].kwargs['phase']}'"
        )


@pytest.mark.asyncio
async def test_release_cap_custom_max_cycles(tmp_path):
    """Explicit max_refinement_cycles=3 loops back until prior_releases reaches 3."""
    pm = _make_pool_for_handle_result(tmp_path)
    queue = JobQueue()

    # With 2 prior releases and cap=3, should still loop back
    status = _status_with_history(prior_release_count=2, max_cycles=3)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = _make_release_result()

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls = pm.blackboard.update_status.call_args_list
    final_phase_calls = [c for c in calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert final_phase_calls[-1].kwargs["phase"] == "submitted"

    # Now at exactly cap=3, should terminate
    pm.blackboard.reset_mock()
    status3 = _status_with_history(prior_release_count=3, max_cycles=3)
    pm.blackboard.get_status.return_value = status3
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls2 = pm.blackboard.update_status.call_args_list
    final_phase_calls2 = [c for c in calls2 if c.kwargs.get("phase") in ("released", "submitted")]
    assert final_phase_calls2[-1].kwargs["phase"] == "released"


@pytest.mark.asyncio
async def test_release_cap_stage_results_behavior(tmp_path):
    """stage_results={} is passed on loop-back but NOT passed on terminal release."""
    pm = _make_pool_for_handle_result(tmp_path)
    queue = JobQueue()

    # Loop-back case: stage_results must be cleared
    status_loopback = _status_with_history(prior_release_count=0)
    pm.blackboard.get_status.return_value = status_loopback
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(_make_release_result(), queue)

    loopback_calls = pm.blackboard.update_status.call_args_list
    phase_calls = [c for c in loopback_calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert phase_calls[-1].kwargs["phase"] == "submitted"
    assert phase_calls[-1].kwargs.get("stage_results") == {}, (
        "Loop-back must clear stage_results to reset pipeline state"
    )

    # Terminal case: stage_results must NOT be cleared (preserve terminal record)
    pm.blackboard.reset_mock()
    status_terminal = _status_with_history(prior_release_count=1)
    pm.blackboard.get_status.return_value = status_terminal
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(_make_release_result(), queue)

    terminal_calls = pm.blackboard.update_status.call_args_list
    phase_calls2 = [c for c in terminal_calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert phase_calls2[-1].kwargs["phase"] == "released"
    assert "stage_results" not in phase_calls2[-1].kwargs, (
        "Terminal release must NOT clear stage_results — preserve the final record"
    )
