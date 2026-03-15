"""Tests for PoolManager scheduling algorithm."""

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call

import pytest

from incubator.core.registry import AgentConfig, Registry
from incubator.orchestrator.pool import PoolManager, PoolState, WindowState, RoleHealth
from incubator.orchestrator.worker import RunResult, RunStatus


def _mock_registry(max_concurrent=1):
    """Create a mock registry where all agents have the given max_concurrent."""
    reg = MagicMock()
    agent_mock = MagicMock()
    agent_mock.max_concurrent = max_concurrent
    agent_mock.phase = None
    agent_mock.status = "active"
    reg.get_agent.return_value = agent_mock
    return reg


def _make_registry(roles: list[str]) -> Registry:
    """Build a Registry with AgentConfigs for the given role names."""
    reg = Registry()
    for role in roles:
        reg.agents[role] = AgentConfig(name=role, description=f"{role} agent", phase=role)
    return reg


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.pool_size = 3
    s.cycle_time_minutes = 30
    s.blackboard_dir = Path("/tmp/test-bb")
    s.registry_path = Path("/tmp/test-registry.yaml")
    s.project_root = Path("/tmp/test-project")
    s.telegram_bot_token = "test"
    s.telegram_chat_id = "test"
    s.pool_dir = Path("/tmp/test-pool")
    return s


def test_build_work_queue_basic():
    """Scheduler builds role->idea assignments from eligible pairs."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry()
    pm.roles = ["ideation", "implementation", "validation"]
    pm.registry = _make_registry(pm.roles)

    # Two ideas: both need ideation, one needs implementation
    ideas = [
        {
            "id": "idea-a", "phase": "submitted",
            "priority_score": 8.0,
            "pipeline": {
                "stages": ["ideation", "implementation"],
                "post_ready": [], "gating": {"default": "auto", "overrides": {}},
            },
            "last_serviced_by": {},
        },
        {
            "id": "idea-b", "phase": "submitted",
            "priority_score": 5.0,
            "pipeline": {
                "stages": ["ideation", "validation"],
                "post_ready": [], "gating": {"default": "auto", "overrides": {}},
            },
            "last_serviced_by": {},
        },
    ]

    pm.blackboard.list_ideas.return_value = ["idea-a", "idea-b"]
    pm.blackboard.get_status.side_effect = lambda id: next(i for i in ideas if i["id"] == id)
    pm.blackboard.get_pipeline.side_effect = lambda id: next(i for i in ideas if i["id"] == id)["pipeline"]
    pm.blackboard.next_stage.side_effect = lambda id: "ideation"  # both need ideation first
    pm.blackboard.is_ready.return_value = False
    pm.blackboard.has_pending_feedback.return_value = False
    pm.blackboard.pipeline_has_role.side_effect = lambda id, role: role in next(
        i for i in ideas if i["id"] == id
    )["pipeline"]["stages"]

    serviced = set()  # (role, idea_id) pairs already done this window
    locked = set()    # idea_ids currently locked

    queue = pm._build_work_queue(ideas, serviced, locked)

    # ideation should pick idea-a (higher priority)
    assert len(queue) >= 1
    assert queue[0] == ("ideation", "idea-a")


def test_build_work_queue_respects_serviced():
    """Scheduler skips role+idea pairs already serviced this window."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry()
    pm.roles = ["ideation"]
    pm.registry = _make_registry(pm.roles)

    ideas = [
        {
            "id": "idea-a", "phase": "submitted", "priority_score": 8.0,
            "pipeline": {"stages": ["ideation"], "post_ready": [], "gating": {"default": "auto", "overrides": {}}},
            "last_serviced_by": {},
        },
    ]

    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False
    pm.blackboard.has_pending_feedback.return_value = False

    serviced = {("ideation", "idea-a")}  # already done
    locked = set()

    queue = pm._build_work_queue(ideas, serviced, locked)
    assert len(queue) == 0


def test_build_work_queue_skips_locked_ideas():
    """Scheduler skips ideas that are currently locked by another worker."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry()
    pm.roles = ["ideation"]
    pm.registry = _make_registry(pm.roles)

    ideas = [
        {
            "id": "idea-a", "phase": "submitted", "priority_score": 8.0,
            "pipeline": {"stages": ["ideation"], "post_ready": [], "gating": {"default": "auto", "overrides": {}}},
            "last_serviced_by": {},
        },
    ]

    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False
    pm.blackboard.has_pending_feedback.return_value = False

    serviced = set()
    locked = {"idea-a"}  # locked by another worker

    queue = pm._build_work_queue(ideas, serviced, locked)
    assert len(queue) == 0


def test_build_work_queue_enforces_pipeline_order_for_not_ready():
    """Not-ready ideas only get their next pipeline stage, not any role."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry()
    pm.roles = ["ideation", "implementation"]
    pm.registry = _make_registry(pm.roles)

    ideas = [
        {
            "id": "idea-a", "phase": "ideation", "priority_score": 8.0,
            "pipeline": {
                "stages": ["ideation", "implementation"],
                "post_ready": [], "gating": {"default": "auto", "overrides": {}},
            },
            "last_serviced_by": {},
        },
    ]

    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"  # ideation is next
    pm.blackboard.is_ready.return_value = False
    pm.blackboard.has_pending_feedback.return_value = False

    serviced = set()
    locked = set()

    queue = pm._build_work_queue(ideas, serviced, locked)

    # Only ideation should appear, not implementation (pipeline order enforced)
    roles_in_queue = [role for role, _ in queue]
    assert "ideation" in roles_in_queue
    assert "implementation" not in roles_in_queue


def test_build_work_queue_ready_ideas_skipped():
    """Ready ideas (all pipeline stages done) are skipped in pipeline pass."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry()
    pm.roles = ["ideation", "competitive", "research"]
    pm.registry = _make_registry(pm.roles)

    ideas = [
        {
            "id": "idea-a", "phase": "released", "priority_score": 8.0,
            "pipeline": {
                "stages": ["ideation", "implementation"],
                "post_ready": ["competitive", "research"],
                "gating": {"default": "auto", "overrides": {}},
            },
            "last_serviced_by": {
                "ideation": "2026-03-11T10:00:00Z",
                "implementation": "2026-03-11T11:00:00Z",
            },
        },
    ]

    pm.blackboard.is_ready.return_value = True
    pm.blackboard.has_pending_feedback.return_value = False

    serviced = set()
    locked = set()

    queue = pm._build_work_queue(ideas, serviced, locked)

    # Ready ideas with no pending feedback get no assignments
    assert len(queue) == 0


def test_build_work_queue_feedback_driven_scheduling():
    """Feedback assigns an agent when idea has no pipeline work queued."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry()
    pm.roles = ["ideation", "implementation"]

    # Two ideas: idea-a needs implementation (pipeline), idea-b is ready but has feedback
    ideas = [
        {
            "id": "idea-a", "phase": "implementation", "priority_score": 8.0,
            "pipeline": {
                "stages": ["ideation", "implementation"],
                "post_ready": [], "gating": {"default": "auto", "overrides": {}},
            },
            "last_serviced_by": {"ideation": "2026-03-11T10:00:00Z"},
        },
        {
            "id": "idea-b", "phase": "implementation", "priority_score": 6.0,
            "pipeline": {
                "stages": ["ideation", "implementation"],
                "post_ready": [], "gating": {"default": "auto", "overrides": {}},
            },
            "last_serviced_by": {"ideation": "2026-03-11T10:00:00Z"},
        },
    ]

    pm.blackboard.next_stage.side_effect = lambda id: "implementation" if id == "idea-a" else None
    pm.blackboard.is_ready.side_effect = lambda id: id == "idea-b"
    # idea-b has pending feedback on ideation
    pm.blackboard.has_pending_feedback.side_effect = lambda id, role: id == "idea-b" and role == "ideation"

    queue = pm._build_work_queue(ideas, set(), set())

    # idea-a gets implementation (pipeline), idea-b gets ideation (feedback)
    assert ("implementation", "idea-a") in queue
    assert ("ideation", "idea-b") in queue


def test_build_work_queue_feedback_skips_serviced():
    """Feedback-driven work is skipped if already serviced this window."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry()
    pm.roles = ["ideation"]

    ideas = [
        {
            "id": "idea-a", "phase": "implementation", "priority_score": 8.0,
        },
    ]

    # Idea is ready (no pipeline work), but has pending feedback on ideation
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.has_pending_feedback.return_value = True

    # ideation already serviced this window — feedback pass should skip it
    serviced = {("ideation", "idea-a")}
    locked = set()

    queue = pm._build_work_queue(ideas, serviced, locked)
    assert len(queue) == 0


def test_build_work_queue_max_concurrent():
    """max_concurrent allows multiple ideas per role in one pass."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
    pm.registry = _mock_registry(max_concurrent=2)  # allow 2 concurrent ideation
    pm.roles = ["ideation"]

    ideas = [
        {"id": "idea-a", "phase": "submitted", "priority_score": 8.0},
        {"id": "idea-b", "phase": "submitted", "priority_score": 6.0},
        {"id": "idea-c", "phase": "submitted", "priority_score": 4.0},
    ]

    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False
    pm.blackboard.has_pending_feedback.return_value = False

    queue = pm._build_work_queue(ideas, set(), set())

    # Should pick top 2 (idea-a, idea-b) but not idea-c (max_concurrent=2)
    assert len(queue) == 2
    assert queue[0] == ("ideation", "idea-a")
    assert queue[1] == ("ideation", "idea-b")


def test_window_state_tracks_serviced():
    """WindowState correctly tracks serviced role+idea pairs."""
    ws = WindowState(
        started_at=datetime.now(timezone.utc),
        cycle_time_minutes=30,
    )
    assert not ws.is_serviced("ideation", "idea-a")
    ws.mark_serviced("ideation", "idea-a")
    assert ws.is_serviced("ideation", "idea-a")


def test_window_state_expiry():
    """WindowState correctly detects expired windows."""
    past = datetime.now(timezone.utc) - timedelta(minutes=31)
    ws = WindowState(started_at=past, cycle_time_minutes=30)
    assert ws.is_expired

    future = datetime.now(timezone.utc) - timedelta(minutes=5)
    ws2 = WindowState(started_at=future, cycle_time_minutes=30)
    assert not ws2.is_expired


# ---------------------------------------------------------------------------
# Release cap logic — tests for the max_refinement_cycles fix in _handle_result
# ---------------------------------------------------------------------------

def _make_pool_for_handle_result(tmp_path):
    """Build a minimal PoolManager suitable for _handle_result tests."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=2, cycle_time_minutes=30, project_root=tmp_path)
    pm.blackboard = MagicMock()
    pm.lock_manager = MagicMock()
    pm.roles = ["ideation", "implementation", "validation", "release"]
    pm.role_health = defaultdict(RoleHealth)
    pm.deadline_counts = defaultdict(int)
    pm.window = WindowState(started_at=datetime.now(timezone.utc), cycle_time_minutes=30)
    pm.pool_dir = tmp_path / "pool"
    pm.pool_dir.mkdir(exist_ok=True)
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

    # 1 prior release, default cap = 1 → at cap → must terminate
    status = _status_with_history(prior_release_count=1)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_stage.return_value = None   # no next stage
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = _make_release_result()

    with patch.object(pm, "_apply_gating", new=AsyncMock()):
        with patch("incubator.orchestrator.pool.broadcast_event", new=AsyncMock(), create=True):
            await pm._handle_result(result)

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

    # 0 prior releases, default cap = 1 → under cap → loop back
    status = _status_with_history(prior_release_count=0)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_stage.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = _make_release_result()

    with patch.object(pm, "_apply_gating", new=AsyncMock()):
        with patch("incubator.orchestrator.pool.broadcast_event", new=AsyncMock(), create=True):
            await pm._handle_result(result)

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
    """Default max_refinement_cycles=1: first run (0 prior) loops, second run (1 prior) terminates."""
    pm = _make_pool_for_handle_result(tmp_path)

    for prior_releases, expected_phase in [(0, "submitted"), (1, "released")]:
        pm.blackboard.reset_mock()
        status = _status_with_history(prior_release_count=prior_releases)
        pm.blackboard.get_status.return_value = status
        pm.blackboard.next_stage.return_value = None
        pm.blackboard.is_ready.return_value = True
        pm.blackboard.get_gating_mode.return_value = "auto"

        result = _make_release_result()

        with patch.object(pm, "_apply_gating", new=AsyncMock()):
            with patch("incubator.orchestrator.pool.broadcast_event", new=AsyncMock(), create=True):
                await pm._handle_result(result)

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

    # With 2 prior releases and cap=3, should still loop back
    status = _status_with_history(prior_release_count=2, max_cycles=3)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_stage.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = _make_release_result()

    with patch.object(pm, "_apply_gating", new=AsyncMock()):
        with patch("incubator.orchestrator.pool.broadcast_event", new=AsyncMock(), create=True):
            await pm._handle_result(result)

    calls = pm.blackboard.update_status.call_args_list
    final_phase_calls = [c for c in calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert final_phase_calls[-1].kwargs["phase"] == "submitted"

    # Now at exactly cap=3, should terminate
    pm.blackboard.reset_mock()
    status3 = _status_with_history(prior_release_count=3, max_cycles=3)
    pm.blackboard.get_status.return_value = status3
    pm.blackboard.next_stage.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    with patch.object(pm, "_apply_gating", new=AsyncMock()):
        with patch("incubator.orchestrator.pool.broadcast_event", new=AsyncMock(), create=True):
            await pm._handle_result(result)

    calls2 = pm.blackboard.update_status.call_args_list
    final_phase_calls2 = [c for c in calls2 if c.kwargs.get("phase") in ("released", "submitted")]
    assert final_phase_calls2[-1].kwargs["phase"] == "released"


@pytest.mark.asyncio
async def test_release_cap_stage_results_behavior(tmp_path):
    """stage_results={} is passed on loop-back but NOT passed on terminal release."""
    pm = _make_pool_for_handle_result(tmp_path)

    # Loop-back case: stage_results must be cleared
    status_loopback = _status_with_history(prior_release_count=0)
    pm.blackboard.get_status.return_value = status_loopback
    pm.blackboard.next_stage.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    with patch.object(pm, "_apply_gating", new=AsyncMock()):
        with patch("incubator.orchestrator.pool.broadcast_event", new=AsyncMock(), create=True):
            await pm._handle_result(_make_release_result())

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
    pm.blackboard.next_stage.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    with patch.object(pm, "_apply_gating", new=AsyncMock()):
        with patch("incubator.orchestrator.pool.broadcast_event", new=AsyncMock(), create=True):
            await pm._handle_result(_make_release_result())

    terminal_calls = pm.blackboard.update_status.call_args_list
    phase_calls2 = [c for c in terminal_calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert phase_calls2[-1].kwargs["phase"] == "released"
    assert "stage_results" not in phase_calls2[-1].kwargs, (
        "Terminal release must NOT clear stage_results — preserve the final record"
    )
