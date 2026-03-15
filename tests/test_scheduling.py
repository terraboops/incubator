"""Integration tests for pool scheduling edge cases."""

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from incubator.core.registry import AgentConfig, Registry
from incubator.orchestrator.pool import PoolManager, WindowState, RoleHealth

_MOCK_PIPELINE = {"stages": ["ideation", "implementation", "validation", "release"], "post_ready": [], "gating": {"default": "auto"}}


def _mock_registry(max_concurrent=1):
    reg = MagicMock()
    agent_mock = MagicMock()
    agent_mock.max_concurrent = max_concurrent
    reg.get_agent.return_value = agent_mock
    return reg


@pytest.fixture
def pool_with_ideas(tmp_path):
    """Create a PoolManager with mock blackboard containing test ideas."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(
        pool_size=2, cycle_time_minutes=30,
        project_root=tmp_path,
    )
    pm.blackboard = MagicMock()
    pm.blackboard.has_pending_feedback.return_value = False
    pm.registry = _mock_registry()
    pm.lock_manager = MagicMock()
    pm.roles = ["ideation", "implementation", "validation"]
    pm.role_health = defaultdict(RoleHealth)
    pm.deadline_counts = defaultdict(int)
    pm.window = None
    pm.pool_dir = tmp_path / "pool"
    pm.pool_dir.mkdir(exist_ok=True)

    # Build a registry matching the default roles
    registry = Registry()
    for role in pm.roles:
        registry.agents[role] = AgentConfig(name=role, description=f"{role} agent", phase=role)
    pm.registry = registry

    return pm


def test_empty_pool_no_crash(pool_with_ideas):
    """Pool handles zero active ideas gracefully."""
    pm = pool_with_ideas
    pm.blackboard.list_ideas.return_value = []
    ideas = pm._get_active_ideas()
    assert ideas == []
    queue = pm._build_work_queue([], set(), set())
    assert queue == []


def test_single_idea_gets_next_stage(pool_with_ideas):
    """Single idea gets its next pipeline stage assigned."""
    pm = pool_with_ideas
    ideas = [{
        "id": "test", "phase": "submitted", "priority_score": 7.0,
        "_effective_priority": 8.0,
    }]
    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False

    queue = pm._build_work_queue(ideas, set(), set())
    assert ("ideation", "test") in queue


def test_killed_ideas_excluded(pool_with_ideas):
    """Killed ideas are not included in active ideas."""
    pm = pool_with_ideas
    pm.blackboard.list_ideas.return_value = ["alive", "dead"]
    pm.blackboard.get_status.side_effect = lambda id: (
        {"id": id, "phase": "killed", "priority_score": 9.0} if id == "dead"
        else {"id": id, "phase": "ideation", "priority_score": 5.0, "pipeline": _MOCK_PIPELINE}
    )
    ideas = pm._get_active_ideas()
    assert len(ideas) == 1
    assert ideas[0]["id"] == "alive"


def test_mid_window_idea_creation(pool_with_ideas):
    """New idea created mid-window is picked up on next _get_active_ideas call."""
    pm = pool_with_ideas

    # First call: no ideas
    pm.blackboard.list_ideas.return_value = []
    ideas1 = pm._get_active_ideas()
    assert len(ideas1) == 0

    # Second call: idea appeared
    pm.blackboard.list_ideas.return_value = ["new-idea"]
    pm.blackboard.get_status.return_value = {
        "id": "new-idea", "phase": "submitted", "priority_score": 7.0, "pipeline": _MOCK_PIPELINE,
    }
    ideas2 = pm._get_active_ideas()
    assert len(ideas2) == 1


def test_crash_recovery_releases_stale_locks(pool_with_ideas):
    """Crash recovery releases locks from dead workers in snapshot."""
    pm = pool_with_ideas

    # Write a snapshot with a running worker
    state = {
        "pool_size": 2,
        "cycle_time_minutes": 30,
        "current_window": {
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "serviced": [{"role": "ideation", "idea_id": "test-idea"}],
        },
        "workers": [
            {"id": 1, "role": "ideation", "idea_id": "test-idea", "idle": False, "started_at": "2026-03-11T10:00:00Z"},
            {"id": 2, "idle": True},
        ],
        "role_health": {"ideation": {"expected": 5, "actual": 4}},
        "deadline_counts": {"implementation": 2},
    }
    (pm.pool_dir / "state.json").write_text(json.dumps(state))

    pm._recover_from_snapshot()

    # Should have released the stale lock for test-idea
    pm.lock_manager.release.assert_called_once_with("pool", "test-idea")
    # Should have restored deadline counts
    assert pm.deadline_counts["implementation"] == 2
    # Should have resumed the window (still valid — only 5 min old)
    assert pm.window is not None
    assert pm.window.is_serviced("ideation", "test-idea")


def test_crash_recovery_starts_fresh_when_window_expired(pool_with_ideas):
    """Crash recovery starts fresh when snapshot window has expired."""
    pm = pool_with_ideas

    state = {
        "current_window": {
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(),
            "serviced": [],
        },
        "workers": [],
        "role_health": {},
        "deadline_counts": {},
    }
    (pm.pool_dir / "state.json").write_text(json.dumps(state))

    pm._recover_from_snapshot()

    # Window should NOT be resumed (it's expired)
    assert pm.window is None


def test_priority_ordering(pool_with_ideas):
    """Higher priority ideas are assigned first."""
    pm = pool_with_ideas
    ideas = [
        {"id": "low", "phase": "ideation", "priority_score": 3.0, "_effective_priority": 3.0},
        {"id": "high", "phase": "ideation", "priority_score": 9.0, "_effective_priority": 9.0},
    ]
    # Sort by priority (descending) — simulates what _get_active_ideas does
    ideas.sort(key=lambda s: s["_effective_priority"], reverse=True)

    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False

    queue = pm._build_work_queue(ideas, set(), set())
    # The first assignment for "ideation" role should be the high-priority idea
    ideation_assignments = [(r, i) for r, i in queue if r == "ideation"]
    assert ideation_assignments[0] == ("ideation", "high")


def test_star_phase_schedules_global_agent(pool_with_ideas):
    """Agent with phase='*' should be scheduled with __all__ sentinel."""
    pm = pool_with_ideas
    from incubator.core.registry import AgentConfig
    ac = AgentConfig(name="artifact-check", description="QA", phase="*", status="active")
    ac.cadence = "*/5 * * * *"
    pm.registry.agents["artifact-check"] = ac
    pm.roles = list(pm.registry.agents.keys())

    ideas = [{
        "id": "test", "phase": "submitted", "priority_score": 7.0,
        "_effective_priority": 8.0,
    }]
    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False

    queue = pm._build_work_queue(ideas, set(), set())
    star_items = [(r, i) for r, i in queue if r == "artifact-check"]
    assert len(star_items) == 1
    assert star_items[0][1] == "__all__"


def test_star_phase_not_scheduled_when_no_ideas(pool_with_ideas):
    """Agent with phase='*' should NOT be scheduled when there are no ideas."""
    pm = pool_with_ideas
    from incubator.core.registry import AgentConfig
    ac = AgentConfig(name="artifact-check", description="QA", phase="*", status="active")
    pm.registry.agents["artifact-check"] = ac
    pm.roles = list(pm.registry.agents.keys())

    queue = pm._build_work_queue([], set(), set())
    star_items = [(r, i) for r, i in queue if r == "artifact-check"]
    assert len(star_items) == 0


def test_star_phase_not_scheduled_when_already_serviced(pool_with_ideas):
    """Agent with phase='*' should NOT be scheduled if already serviced this window."""
    pm = pool_with_ideas
    from incubator.core.registry import AgentConfig
    ac = AgentConfig(name="artifact-check", description="QA", phase="*", status="active")
    pm.registry.agents["artifact-check"] = ac
    pm.roles = list(pm.registry.agents.keys())

    ideas = [{"id": "test", "phase": "submitted", "priority_score": 7.0, "_effective_priority": 8.0}]
    serviced = {("artifact-check", "__all__")}

    pm.blackboard.pipeline_has_role.return_value = True
    pm.blackboard.next_stage.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False

    queue = pm._build_work_queue(ideas, serviced, set())
    star_items = [(r, i) for r, i in queue if r == "artifact-check"]
    assert len(star_items) == 0


def test_early_stage_boost(pool_with_ideas):
    """Submitted/ideation ideas get a +1.0 priority boost."""
    pm = pool_with_ideas
    pm.blackboard.list_ideas.return_value = ["new", "mature"]
    pm.blackboard.get_status.side_effect = lambda id: (
        {"id": id, "phase": "submitted", "priority_score": 5.0, "pipeline": _MOCK_PIPELINE} if id == "new"
        else {"id": id, "phase": "implementation", "priority_score": 5.0, "pipeline": _MOCK_PIPELINE}
    )
    ideas = pm._get_active_ideas()
    new_idea = next(i for i in ideas if i["id"] == "new")
    mature_idea = next(i for i in ideas if i["id"] == "mature")
    assert new_idea["_effective_priority"] == 6.0  # 5.0 + 1.0 boost
    assert mature_idea["_effective_priority"] == 5.0  # no boost
