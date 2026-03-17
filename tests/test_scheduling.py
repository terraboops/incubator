"""Tests for the redesigned pool scheduler: JobQueue, CadenceTracker, PoolManager."""

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from incubator.core.registry import AgentConfig, Registry
from incubator.orchestrator.job_queue import (
    Job, JobQueue, CadenceTracker, compute_priority,
    PRIORITY_DEFAULT, PRIORITY_EARLY_BOOST, MAX_BACKGROUND_PRIORITY,
    FEEDBACK_PRIORITY_FACTOR,
)
from incubator.orchestrator.pool import PoolManager, can_schedule
from incubator.orchestrator.worker import RunResult, RunStatus


# ── JobQueue tests ───────────────────────────────────────────────────

class TestJobQueue:
    def test_enqueue_and_pop_priority_order(self):
        """Jobs pop in highest-priority-first order."""
        q = JobQueue()
        q.enqueue(Job(priority=3.0, kind="pipeline", role="a", idea_id="x"))
        q.enqueue(Job(priority=8.0, kind="pipeline", role="b", idea_id="y"))
        q.enqueue(Job(priority=5.0, kind="pipeline", role="c", idea_id="z"))

        jobs = []
        while (j := q.pop()) is not None:
            jobs.append(j)

        assert [j.priority for j in jobs] == [8.0, 5.0, 3.0]

    def test_dedup_prevents_double_enqueue(self):
        """Same (role, idea_id) can't be enqueued twice."""
        q = JobQueue()
        assert q.enqueue(Job(priority=5.0, kind="pipeline", role="a", idea_id="x"))
        assert not q.enqueue(Job(priority=9.0, kind="pipeline", role="a", idea_id="x"))
        assert q.depth == 1

    def test_mark_done_allows_re_enqueue(self):
        """After mark_done, same (role, idea_id) can be enqueued again."""
        q = JobQueue()
        q.enqueue(Job(priority=5.0, kind="pipeline", role="a", idea_id="x"))
        q.pop()
        q.mark_done("a", "x")
        assert q.enqueue(Job(priority=7.0, kind="pipeline", role="a", idea_id="x"))

    def test_cancel_removes_from_active(self):
        """Cancelled jobs are skipped on pop."""
        q = JobQueue()
        q.enqueue(Job(priority=5.0, kind="pipeline", role="a", idea_id="x"))
        q.enqueue(Job(priority=3.0, kind="pipeline", role="b", idea_id="y"))
        q.cancel("a", "x")
        job = q.pop()
        assert job.role == "b"
        assert q.pop() is None

    def test_empty_queue_returns_none(self):
        q = JobQueue()
        assert q.pop() is None

    def test_depth_tracks_active_count(self):
        q = JobQueue()
        assert q.depth == 0
        q.enqueue(Job(priority=5.0, kind="pipeline", role="a", idea_id="x"))
        assert q.depth == 1
        q.enqueue(Job(priority=3.0, kind="pipeline", role="b", idea_id="y"))
        assert q.depth == 2
        q.pop()
        # Still active (not mark_done yet)
        assert q.depth == 2
        q.mark_done("a", "x")
        assert q.depth == 1


# ── CadenceTracker tests ────────────────────────────────────────────

class TestCadenceTracker:
    def test_never_run_is_at_deadline(self):
        """Agent that has never run should be treated as at deadline."""
        ct = CadenceTracker("watcher", "0 */6 * * *")
        assert ct.elapsed_ratio() == 1.0
        assert ct.is_due()

    def test_just_ran_is_zero(self):
        """Agent that just ran should have ~0 elapsed ratio."""
        ct = CadenceTracker("watcher", "0 */6 * * *")
        now = datetime.now(timezone.utc)
        ct.last_run_at = now
        ratio = ct.elapsed_ratio(now)
        assert ratio == pytest.approx(0.0, abs=0.01)

    def test_halfway_through_cadence(self):
        """At 50% through cadence, ratio should be ~0.5."""
        # Use a simple "every hour" cadence and measure at 30 min
        ct = CadenceTracker("watcher", "0 * * * *")  # every hour, on the hour
        # Set last_run_at to exactly on an hour boundary
        now = datetime(2026, 3, 15, 12, 30, 0, tzinfo=timezone.utc)
        ct.last_run_at = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        ratio = ct.elapsed_ratio(now)
        assert 0.4 < ratio < 0.6

    def test_overdue_exceeds_one(self):
        """Past cadence deadline, ratio > 1.0."""
        ct = CadenceTracker("watcher", "0 */6 * * *")
        now = datetime.now(timezone.utc)
        ct.last_run_at = now - timedelta(hours=12)  # 12h ago = 200% of 6h
        ratio = ct.elapsed_ratio(now)
        assert ratio > 1.0

    def test_priority_ramps_with_ratio(self):
        """Priority should scale linearly with elapsed ratio."""
        ct = CadenceTracker("watcher", "0 */6 * * *")
        now = datetime.now(timezone.utc)

        # Just ran → ~0
        ct.last_run_at = now
        assert ct.priority(now) == pytest.approx(0.0, abs=0.1)

        # At deadline → 10.0
        ct.last_run_at = None
        assert ct.priority(now) == MAX_BACKGROUND_PRIORITY

    def test_priority_caps_at_max(self):
        """Priority should not exceed MAX_BACKGROUND_PRIORITY."""
        ct = CadenceTracker("watcher", "0 */6 * * *")
        now = datetime.now(timezone.utc)
        ct.last_run_at = now - timedelta(hours=24)  # way overdue
        assert ct.priority(now) == MAX_BACKGROUND_PRIORITY

    def test_error_reset_prevents_livelock(self):
        """Resetting last_run_at on error prevents infinite retry."""
        ct = CadenceTracker("watcher", "0 */6 * * *")
        now = datetime.now(timezone.utc)
        ct.last_run_at = None  # never run = due

        assert ct.is_due(now)

        # Simulate error: reset cadence
        ct.last_run_at = now
        assert not ct.is_due(now)


# ── compute_priority tests ───────────────────────────────────────────

class TestComputePriority:
    def test_pipeline_uses_idea_priority(self):
        assert compute_priority("pipeline", idea_priority=7.0) == 7.0

    def test_pipeline_early_boost(self):
        assert compute_priority("pipeline", idea_priority=7.0, is_first_agent=True) == 8.0

    def test_feedback_below_pipeline(self):
        pri = compute_priority("feedback", idea_priority=7.0)
        assert pri == pytest.approx(7.0 * FEEDBACK_PRIORITY_FACTOR)

    def test_background_uses_tracker(self):
        ct = CadenceTracker("watcher", "0 */6 * * *")
        ct.last_run_at = None
        pri = compute_priority("background", cadence_tracker=ct)
        assert pri == MAX_BACKGROUND_PRIORITY


# ── can_schedule tests ───────────────────────────────────────────────

class TestCanSchedule:
    def test_different_groups_can_overlap(self):
        """Agents in different parallel groups can run on the same idea."""
        pipeline = {
            "agents": ["ideation", "implementation"],
            "parallel_groups": [
                ["ideation", "implementation"],
                ["competitive-watcher"],
            ],
        }
        running = {("ideation", "idea-a")}
        assert can_schedule("competitive-watcher", "idea-a", running, pipeline)

    def test_same_group_blocked(self):
        """Agents in the same parallel group are serialized on an idea."""
        pipeline = {
            "agents": ["ideation", "implementation"],
            "parallel_groups": [
                ["ideation", "implementation"],
                ["competitive-watcher"],
            ],
        }
        running = {("ideation", "idea-a")}
        assert not can_schedule("implementation", "idea-a", running, pipeline)

    def test_different_ideas_always_allowed(self):
        """Same agent can run on different ideas."""
        pipeline = {
            "agents": ["ideation"],
            "parallel_groups": [["ideation"]],
        }
        running = {("ideation", "idea-b")}
        assert can_schedule("ideation", "idea-a", running, pipeline)

    def test_global_agent_always_allowed(self):
        """Global agents (__all__) are always schedulable."""
        pipeline = {"agents": ["ideation"], "parallel_groups": [["ideation"]]}
        running = {("ideation", "idea-a")}
        assert can_schedule("global-agent", "__all__", running, pipeline)

    def test_unknown_role_always_allowed(self):
        """Roles not in any group can always run."""
        pipeline = {
            "agents": ["ideation"],
            "parallel_groups": [["ideation"]],
        }
        running = {("ideation", "idea-a")}
        assert can_schedule("unknown-role", "idea-a", running, pipeline)

    def test_empty_running_set(self):
        """No running jobs — everything is schedulable."""
        pipeline = {
            "agents": ["ideation", "implementation"],
            "parallel_groups": [["ideation", "implementation"]],
        }
        assert can_schedule("ideation", "idea-a", set(), pipeline)


# ── PoolManager._handle_result tests ────────────────────────────────

def _make_pool(tmp_path):
    """Build a minimal PoolManager for _handle_result tests."""
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
        m.phase = name  # pipeline agents have phase matching their name
        m.max_concurrent = 1
        return m
    pm.registry = MagicMock()
    pm.registry.get_agent.side_effect = _get_agent
    pm.pool_dir = tmp_path / "pool"
    pm.pool_dir.mkdir(exist_ok=True)
    pm.workers = []
    return pm


def _status_with_history(prior_release_count: int, max_cycles: int | None = None) -> dict:
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
async def test_handle_result_release_terminates_at_cap(tmp_path):
    """When prior_releases >= max_refinement_cycles, phase is set to terminal 'released'."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    status = _status_with_history(prior_release_count=1)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(status=RunStatus.OK, role="release", idea_id="test-idea",
                       duration_seconds=10.0, cost_usd=0.05)

    with patch("incubator.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls = pm.blackboard.update_status.call_args_list
    phase_calls = [c for c in calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert phase_calls, "Should have set a phase"
    assert phase_calls[-1].kwargs["phase"] == "released"


@pytest.mark.asyncio
async def test_handle_result_release_loops_under_cap(tmp_path):
    """When prior_releases < max_refinement_cycles, phase resets for refinement."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    status = _status_with_history(prior_release_count=0)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(status=RunStatus.OK, role="release", idea_id="test-idea",
                       duration_seconds=10.0, cost_usd=0.05)

    with patch("incubator.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls = pm.blackboard.update_status.call_args_list
    phase_calls = [c for c in calls if c.kwargs.get("phase") in ("released", "submitted")]
    assert phase_calls, "Should have set a phase"
    assert phase_calls[-1].kwargs["phase"] == "submitted"
    assert phase_calls[-1].kwargs.get("stage_results") == {}


@pytest.mark.asyncio
async def test_handle_result_error_allows_retry(tmp_path):
    """Error results don't block re-enqueue — mark_done lets pipeline_producer re-add."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm.blackboard.get_status.return_value = {
        "id": "test-idea", "phase": "ideation",
        "last_serviced_by": {}, "stage_results": {},
    }

    result = RunResult(status=RunStatus.ERROR, role="ideation", idea_id="test-idea",
                       error="agent crashed")

    with patch("incubator.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    # Error should update status with error info
    calls = pm.blackboard.update_status.call_args_list
    assert any(c.kwargs.get("last_error") for c in calls)


@pytest.mark.asyncio
async def test_handle_result_advances_phase(tmp_path):
    """Successful completion with 'proceed' advances to next agent."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm.blackboard.get_status.return_value = {
        "id": "test-idea", "phase": "ideation",
        "phase_history": [],
        "last_serviced_by": {},
        "total_cost_usd": 0.0,
        "iteration_count": 0,
        "stage_results": {},
        "phase_recommendation": "proceed",
    }
    pm.blackboard.next_agent.return_value = "implementation"
    pm.blackboard.is_ready.return_value = False
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(status=RunStatus.OK, role="ideation", idea_id="test-idea",
                       duration_seconds=60.0, cost_usd=0.10)

    with patch("incubator.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls = pm.blackboard.update_status.call_args_list
    phase_calls = [c for c in calls if c.kwargs.get("phase") == "implementation"]
    assert phase_calls, "Should advance phase to implementation"


@pytest.mark.asyncio
async def test_handle_result_kill_recommendation(tmp_path):
    """Kill recommendation sets phase to killed."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm.blackboard.get_status.return_value = {
        "id": "test-idea", "phase": "ideation",
        "phase_history": [],
        "last_serviced_by": {},
        "total_cost_usd": 0.0,
        "iteration_count": 0,
        "stage_results": {},
        "phase_recommendation": "kill",
    }
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(status=RunStatus.OK, role="ideation", idea_id="test-idea",
                       duration_seconds=30.0, cost_usd=0.05)

    with patch("incubator.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls = pm.blackboard.update_status.call_args_list
    phase_calls = [c for c in calls if c.kwargs.get("phase") == "killed"]
    assert phase_calls, "Should set phase to killed"


@pytest.mark.asyncio
async def test_handle_result_human_review_gating(tmp_path):
    """Human-review gating mode sets needs_human_review."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm.blackboard.get_status.return_value = {
        "id": "test-idea", "phase": "ideation",
        "phase_history": [],
        "last_serviced_by": {},
        "total_cost_usd": 0.0,
        "iteration_count": 0,
        "stage_results": {},
        "phase_recommendation": "proceed",
    }
    pm.blackboard.get_gating_mode.return_value = "human-review"

    result = RunResult(status=RunStatus.OK, role="ideation", idea_id="test-idea",
                       duration_seconds=30.0, cost_usd=0.05)

    with patch("incubator.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    calls = pm.blackboard.update_status.call_args_list
    review_calls = [c for c in calls if c.kwargs.get("needs_human_review") is True]
    assert review_calls, "Should set needs_human_review"


# ── Pipeline producer tests ──────────────────────────────────────────

def test_pipeline_producer_enqueues_next_agent(tmp_path):
    """Pipeline producer creates jobs for ideas needing their next agent."""
    pm = _make_pool(tmp_path)
    pm.blackboard.list_ideas.return_value = ["idea-a"]
    pm.blackboard.get_status.return_value = {
        "id": "idea-a", "phase": "submitted",
        "priority_score": 7.0,
        "last_serviced_by": {}, "stage_results": {},
        "pipeline": {
            "agents": ["ideation", "implementation"],
            "parallel_groups": [["ideation", "implementation"]],
            "post_ready": [],
            "gating": {"default": "auto", "overrides": {}},
        },
    }
    pm.blackboard.get_pipeline.return_value = {
        "agents": ["ideation", "implementation"],
        "parallel_groups": [["ideation", "implementation"]],
        "post_ready": [],
        "gating": {"default": "auto", "overrides": {}},
    }
    pm.blackboard.next_agent.return_value = "ideation"
    pm.blackboard.is_ready.return_value = False
    pm.blackboard.has_pending_feedback.return_value = False
    pm.blackboard.pending_post_ready.return_value = []

    queue = JobQueue()
    pm._pipeline_producer(queue)

    job = queue.pop()
    assert job is not None
    assert job.role == "ideation"
    assert job.idea_id == "idea-a"


def test_pipeline_producer_skips_terminal(tmp_path):
    """Pipeline producer skips killed ideas."""
    pm = _make_pool(tmp_path)
    pm.blackboard.list_ideas.return_value = ["idea-a"]
    pm.blackboard.get_status.return_value = {
        "id": "idea-a", "phase": "killed",
        "priority_score": 7.0,
    }
    pm.blackboard.pending_post_ready.return_value = []

    queue = JobQueue()
    pm._pipeline_producer(queue)

    assert queue.pop() is None


# ── Cadence producer tests ───────────────────────────────────────────

def _make_pool_with_watcher(tmp_path):
    """Pool with a cadence-tracked watcher agent."""
    pm = _make_pool(tmp_path)
    def _get_agent(name):
        m = MagicMock()
        m.cadence = "0 */6 * * *" if name == "watcher" else None
        m.phase = None
        m.max_concurrent = 1
        return m
    pm.registry.get_agent.side_effect = _get_agent
    pm.blackboard.list_ideas.return_value = ["idea-a", "idea-b"]
    pm.blackboard.get_status.side_effect = lambda id: {
        "id": id, "phase": "released", "priority_score": 5.0,
    }
    return pm


def test_cadence_producer_enqueues_due_agents(tmp_path):
    """Cadence producer creates per-idea jobs for due background agents."""
    pm = _make_pool_with_watcher(tmp_path)
    queue = JobQueue()

    trackers = {"watcher": CadenceTracker("watcher", "0 */6 * * *")}
    pm._cadence_producer(queue, trackers)

    jobs = []
    while (j := queue.pop()) is not None:
        jobs.append(j)
    assert len(jobs) == 2
    assert {j.idea_id for j in jobs} == {"idea-a", "idea-b"}
    assert all(j.priority == MAX_BACKGROUND_PRIORITY for j in jobs)


def test_cadence_producer_skips_not_due(tmp_path):
    """Cadence producer doesn't enqueue agents that just ran."""
    pm = _make_pool_with_watcher(tmp_path)
    queue = JobQueue()

    trackers = {"watcher": CadenceTracker("watcher", "0 */6 * * *")}
    trackers["watcher"].last_run_at = datetime.now(timezone.utc)

    pm._cadence_producer(queue, trackers)
    assert queue.pop() is None


# ── _pop_schedulable tests ───────────────────────────────────────────

def test_pop_schedulable_respects_max_concurrent(tmp_path):
    """pop_schedulable skips roles that hit max_concurrent."""
    pm = _make_pool(tmp_path)
    pm.registry.get_agent.return_value = MagicMock(max_concurrent=1)

    queue = JobQueue()
    queue.enqueue(Job(priority=8.0, kind="pipeline", role="ideation", idea_id="idea-a"))
    queue.enqueue(Job(priority=5.0, kind="pipeline", role="ideation", idea_id="idea-b"))

    # ideation already running on idea-a
    running = {("ideation", "idea-a")}

    job = pm._pop_schedulable(queue, running)
    # max_concurrent=1 and ideation is already running → skip both
    assert job is None


def test_pop_schedulable_parallel_groups(tmp_path):
    """pop_schedulable allows different-group agents on same idea."""
    pm = _make_pool(tmp_path)
    pm.registry.get_agent.return_value = MagicMock(max_concurrent=5)

    queue = JobQueue()
    queue.enqueue(Job(priority=8.0, kind="background", role="watcher", idea_id="idea-a"))

    pm.blackboard.get_pipeline.return_value = {
        "agents": ["ideation"],
        "parallel_groups": [["ideation"], ["watcher"]],
    }

    running = {("ideation", "idea-a")}
    job = pm._pop_schedulable(queue, running)
    assert job is not None
    assert job.role == "watcher"


# ── _get_active_ideas tests ──────────────────────────────────────────

def test_get_active_ideas_excludes_killed(tmp_path):
    """Killed ideas are excluded."""
    pm = _make_pool(tmp_path)
    pm.blackboard.list_ideas.return_value = ["alive", "dead"]
    pm.blackboard.get_status.side_effect = lambda id: (
        {"id": id, "phase": "killed", "priority_score": 9.0}
        if id == "dead"
        else {"id": id, "phase": "ideation", "priority_score": 5.0}
    )
    pm.blackboard.pending_post_ready.return_value = []
    pm.blackboard.get_pipeline.return_value = {
        "agents": ["ideation"], "parallel_groups": [["ideation"]],
    }
    pm.blackboard.next_agent.return_value = "ideation"
    ideas = pm._get_active_ideas()
    assert len(ideas) == 1
    assert ideas[0]["id"] == "alive"


def test_get_active_ideas_early_boost(tmp_path):
    """Submitted ideas at first pipeline agent get priority boost."""
    pm = _make_pool(tmp_path)
    pm.blackboard.list_ideas.return_value = ["new"]
    pm.blackboard.get_status.return_value = {
        "id": "new", "phase": "submitted", "priority_score": 5.0,
    }
    pm.blackboard.pending_post_ready.return_value = []
    pm.blackboard.get_pipeline.return_value = {
        "agents": ["ideation", "implementation"],
        "parallel_groups": [["ideation", "implementation"]],
    }
    pm.blackboard.next_agent.return_value = "ideation"
    ideas = pm._get_active_ideas()
    assert ideas[0]["_effective_priority"] == 6.0  # 5.0 + 1.0 boost
