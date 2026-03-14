"""Tests for PoolManager scheduling algorithm."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from incubator.core.registry import AgentConfig, Registry
from incubator.orchestrator.pool import PoolManager, PoolState, WindowState


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

    serviced = {("ideation", "idea-a")}  # already done
    locked = set()

    queue = pm._build_work_queue(ideas, serviced, locked)
    assert len(queue) == 0


def test_build_work_queue_skips_locked_ideas():
    """Scheduler skips ideas that are currently locked by another worker."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
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

    serviced = set()
    locked = {"idea-a"}  # locked by another worker

    queue = pm._build_work_queue(ideas, serviced, locked)
    assert len(queue) == 0


def test_build_work_queue_enforces_pipeline_order_for_not_ready():
    """Not-ready ideas only get their next pipeline stage, not any role."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
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

    serviced = set()
    locked = set()

    queue = pm._build_work_queue(ideas, serviced, locked)

    # Only ideation should appear, not implementation (pipeline order enforced)
    roles_in_queue = [role for role, _ in queue]
    assert "ideation" in roles_in_queue
    assert "implementation" not in roles_in_queue


def test_build_work_queue_any_order_for_ready_ideas():
    """Ready ideas accept any role from their pipeline, not just next stage."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(pool_size=3, cycle_time_minutes=30)
    pm.blackboard = MagicMock()
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

    pm.blackboard.pipeline_has_role.side_effect = lambda id, role: (
        role in ideas[0]["pipeline"]["stages"] or role in ideas[0]["pipeline"]["post_ready"]
    )
    pm.blackboard.is_ready.return_value = True

    serviced = set()
    locked = set()

    queue = pm._build_work_queue(ideas, serviced, locked)

    roles_in_queue = [role for role, _ in queue]
    # All three roles should be eligible since idea is ready
    assert "ideation" in roles_in_queue
    assert "competitive" in roles_in_queue
    assert "research" in roles_in_queue


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
