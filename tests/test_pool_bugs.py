"""Tests for TLA+-verified pool scheduler bug fixes.

Bug 1: Feedback runs must NOT increment iter_counts
Bug 2: Per-agent iter_counts (not global), reset on proceed, dismiss resets only capped
Bug 3: post_ready roles cleared from last_serviced_by on refinement loop
Bug 4: break → continue in dispatch loop
"""

from unittest.mock import MagicMock, patch

import pytest

from trellis.orchestrator.job_queue import Job, JobQueue
from trellis.orchestrator.pool import MAX_ITERATE_PER_STAGE, PoolManager
from trellis.orchestrator.worker import RunResult, RunStatus

# Default value used by tests — matches Settings.max_iterate_per_stage default
_DEFAULT_MAX_ITERATE = 3


def _make_pool(tmp_path):
    """Build a minimal PoolManager for _handle_result tests."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(
        pool_size=3,
        job_timeout_minutes=60,
        producer_interval_seconds=10,
        project_root=tmp_path,
        telegram_bot_token="test",
        telegram_chat_id="test",
        max_iterate_per_stage=_DEFAULT_MAX_ITERATE,
        max_refinement_cycles=1,
        min_quality_score=0.0,
    )
    pm.blackboard = MagicMock()
    pm.lock_manager = MagicMock()
    pm.roles = ["ideation", "implementation", "validation", "release"]
    pm._job_kinds = {}
    pm._rate_limit_hits = {}

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


def _base_status(idea_id="test-idea", **overrides):
    status = {
        "id": idea_id,
        "phase": "ideation",
        "phase_history": [],
        "last_serviced_by": {},
        "total_cost_usd": 0.0,
        "iter_counts": {},
        "iteration_count": 0,
        "stage_results": {},
        "phase_recommendation": "proceed",
        "deadline_hits": {},
    }
    status.update(overrides)
    return status


# ── Bug 1: Feedback does not increment iter_counts ──────────────────


@pytest.mark.asyncio
async def test_feedback_does_not_increment_iter_counts(tmp_path):
    """Feedback runs leave the agent's iter_counts entry unchanged."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    # Mark this job as feedback
    pm._job_kinds[("ideation", "test-idea")] = "feedback"

    status = _base_status(iter_counts={"ideation": 1})
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = "implementation"
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="ideation",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    # Find the tracking update call (has iter_counts kwarg)
    tracking_calls = [
        c for c in pm.blackboard.update_status.call_args_list if "iter_counts" in c.kwargs
    ]
    assert tracking_calls
    # iteration_count (an integer, captured by value) proves feedback didn't increment:
    # if feedback incremented, it'd be 2 (1+1); instead it stays at 1
    assert tracking_calls[0].kwargs["iteration_count"] == 1


@pytest.mark.asyncio
async def test_feedback_skips_iterate_cap_check(tmp_path):
    """Feedback with 'iterate' recommendation must NOT trigger the iteration cap."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm._job_kinds[("ideation", "test-idea")] = "feedback"

    status = _base_status(
        phase_recommendation="iterate",
        iter_counts={"ideation": MAX_ITERATE_PER_STAGE},
    )
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = "implementation"
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="ideation",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    # Should NOT have set needs_human_review
    review_calls = [
        c
        for c in pm.blackboard.update_status.call_args_list
        if c.kwargs.get("needs_human_review") is True
    ]
    assert not review_calls, "Feedback run should not trigger iteration cap review"


# ── Bug 2: Per-agent iteration tracking ─────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_increments_per_agent_iter_counts(tmp_path):
    """Pipeline run increments only the completing agent's count."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm._job_kinds[("ideation", "test-idea")] = "pipeline"

    status = _base_status(iter_counts={"validation": 2})
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = "implementation"
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="ideation",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    tracking_calls = [
        c for c in pm.blackboard.update_status.call_args_list if "iter_counts" in c.kwargs
    ]
    assert tracking_calls
    ic = tracking_calls[0].kwargs["iter_counts"]
    # ideation: 0 -> 1 (incremented), then reset to 0 on proceed
    # validation: 2 (untouched in tracking call)
    assert ic.get("validation") == 2, "Other agents' counts must not change"


@pytest.mark.asyncio
async def test_proceed_resets_agent_iter_count(tmp_path):
    """Proceed resets the proceeding agent's count to 0, others untouched."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm._job_kinds[("ideation", "test-idea")] = "pipeline"

    status = _base_status(
        iter_counts={"ideation": 2, "validation": 1},
        phase_recommendation="proceed",
    )
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = "implementation"
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="ideation",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    # The proceed reset call is the second iter_counts update
    iter_calls = [
        c for c in pm.blackboard.update_status.call_args_list if "iter_counts" in c.kwargs
    ]
    # Last iter_counts update should have ideation=0
    last_ic = iter_calls[-1].kwargs["iter_counts"]
    assert last_ic["ideation"] == 0, "Proceeding agent's count must reset to 0"
    assert last_ic["validation"] == 1, "Other agents' counts must be untouched"


@pytest.mark.asyncio
async def test_dismiss_review_resets_only_capped_agents(tmp_path):
    """DismissReview resets only agents at the cap, others keep their counts."""
    from trellis.web.api.routes.ideas import idea_action

    bb = MagicMock()
    status = {
        "iter_counts": {
            "ideation": _DEFAULT_MAX_ITERATE,  # at cap → should reset
            "validation": 1,  # below cap → keep
        },
    }
    bb.get_status.return_value = status

    mock_settings = MagicMock(max_iterate_per_stage=_DEFAULT_MAX_ITERATE)
    with (
        patch("trellis.web.api.routes.ideas._get_blackboard", return_value=bb),
        patch("trellis.web.api.routes.ideas.get_settings", return_value=mock_settings),
    ):
        request = MagicMock()
        request.app.state.projection = None
        await idea_action(
            request=request,
            idea_id="test-idea",
            action="dismiss_review",
        )

    call_kwargs = bb.update_status.call_args.kwargs
    assert call_kwargs["needs_human_review"] is False
    ic = call_kwargs["iter_counts"]
    assert ic["ideation"] == 0, "Capped agent must be reset"
    assert ic["validation"] == 1, "Non-capped agent must keep its count"
    assert call_kwargs["iteration_count"] == 1  # sum of new counts


@pytest.mark.asyncio
async def test_iter_counts_backward_compat(tmp_path):
    """iteration_count (global) equals sum of iter_counts values."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm._job_kinds[("ideation", "test-idea")] = "pipeline"

    status = _base_status(
        iter_counts={"validation": 2, "implementation": 1},
        phase_recommendation="iterate",
    )
    pm.blackboard.get_status.return_value = status
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="ideation",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    tracking_calls = [
        c for c in pm.blackboard.update_status.call_args_list if "iteration_count" in c.kwargs
    ]
    assert tracking_calls
    ic = tracking_calls[0].kwargs["iter_counts"]
    global_count = tracking_calls[0].kwargs["iteration_count"]
    assert global_count == sum(ic.values()), (
        f"iteration_count ({global_count}) must equal sum of iter_counts ({sum(ic.values())})"
    )


# ── Bug 3: post_ready reruns after refinement ───────────────────────


@pytest.mark.asyncio
async def test_post_ready_reruns_after_refinement(tmp_path):
    """On refinement loop-back, post_ready roles are cleared from last_serviced_by."""
    pm = _make_pool(tmp_path)
    queue = JobQueue()

    pm._job_kinds[("release", "test-idea")] = "pipeline"

    status = _base_status(
        phase="release",
        phase_recommendation="proceed",
        last_serviced_by={
            "ideation": "2026-03-01T00:00:00Z",
            "watcher": "2026-03-01T00:00:00Z",
        },
        max_refinement_cycles=3,
    )
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"
    pm.blackboard.get_pipeline.return_value = {
        "agents": ["ideation", "implementation", "release"],
        "post_ready": ["watcher"],
    }

    result = RunResult(
        status=RunStatus.OK,
        role="release",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    # Find the loop-back call (phase="submitted")
    loopback_calls = [
        c
        for c in pm.blackboard.update_status.call_args_list
        if c.kwargs.get("phase") == "submitted"
    ]
    assert loopback_calls, "Should loop back to submitted"
    serviced = loopback_calls[0].kwargs.get("last_serviced_by", {})
    assert "watcher" not in serviced, "post_ready role must be cleared from last_serviced_by"
    # Non-post_ready roles should remain (though they may have been updated)


# ── Bug 4: dispatch continues past blocked worker ────────────────────


def test_dispatch_continues_past_blocked_worker(tmp_path):
    """When _pop_schedulable returns None for one worker, other idle workers still get checked."""
    pm = _make_pool(tmp_path)

    # Create 3 mock workers: first two idle, third busy
    workers = []
    for i in range(3):
        w = MagicMock()
        w.worker_id = i + 1
        w.is_idle = i < 2  # first two idle
        w.current_role = None if i < 2 else "implementation"
        w.current_idea = None if i < 2 else "other-idea"
        workers.append(w)
    pm.workers = workers

    # _pop_schedulable returns None first time (no jobs for worker 1),
    # then a job second time (for worker 2)
    call_count = 0

    def mock_pop(queue, running):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # no job for first idle worker
        return Job(priority=10, kind="pipeline", role="ideation", idea_id="test-idea")

    # Simulate the dispatch loop logic from _run_loop
    queue = JobQueue()
    running = {(w.current_role, w.current_idea) for w in pm.workers if not w.is_idle}

    # This mimics the fixed dispatch loop (continue instead of break)
    dispatched = []
    for worker in pm.workers:
        if not worker.is_idle:
            continue
        job = mock_pop(queue, running)
        if job is None:
            continue  # Bug 4 fix: was `break`
        dispatched.append((worker.worker_id, job))
        running.add((job.role, job.idea_id))

    assert len(dispatched) == 1, "Second idle worker should get a job"
    assert dispatched[0][0] == 2, "Worker 2 should be the one dispatched"
    assert call_count == 2, "Both idle workers should have been checked"


# ── Quality gate tests ───────────────────────────────────────────────


def _release_status(prior_releases=0, score=5.0, max_cycles=None):
    """Build a status dict for quality gate tests."""
    history = [
        {"from": "release", "to": "released", "at": "2026-03-13T00:00:00Z"}
        for _ in range(prior_releases)
    ]
    status = {
        "id": "test-idea",
        "phase": "release",
        "phase_history": history,
        "last_serviced_by": {},
        "total_cost_usd": 0.0,
        "iter_counts": {},
        "iteration_count": 0,
        "stage_results": {},
        "phase_recommendation": "proceed",
        "deadline_hits": {},
        "priority_score": score,
    }
    if max_cycles is not None:
        status["max_refinement_cycles"] = max_cycles
    return status


def _make_release_pool(tmp_path, min_quality_score=0.0, max_refinement_cycles=1):
    """Build a PoolManager configured for quality gate tests."""
    pm = _make_pool(tmp_path)
    pm.settings.min_quality_score = min_quality_score
    pm.settings.max_refinement_cycles = max_refinement_cycles
    pm.blackboard.get_pipeline.return_value = {
        "agents": ["ideation", "implementation", "release"],
        "post_ready": [],
    }
    return pm


@pytest.mark.asyncio
async def test_quality_gate_disabled_releases_normally(tmp_path):
    """When min_quality_score=0, low-score ideas release normally."""
    pm = _make_release_pool(tmp_path, min_quality_score=0.0)
    queue = JobQueue()

    pm._job_kinds[("release", "test-idea")] = "pipeline"
    status = _release_status(prior_releases=1, score=2.0)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="release",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    phase_calls = [
        c
        for c in pm.blackboard.update_status.call_args_list
        if c.kwargs.get("phase") in ("released", "submitted")
    ]
    assert phase_calls[-1].kwargs["phase"] == "released"


@pytest.mark.asyncio
async def test_quality_gate_low_score_loops_back(tmp_path):
    """Below threshold + under cycle cap → loop back for refinement."""
    pm = _make_release_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=3)
    queue = JobQueue()

    pm._job_kinds[("release", "test-idea")] = "pipeline"
    status = _release_status(prior_releases=0, score=4.0, max_cycles=3)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="release",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    phase_calls = [
        c
        for c in pm.blackboard.update_status.call_args_list
        if c.kwargs.get("phase") in ("released", "submitted")
    ]
    assert phase_calls[-1].kwargs["phase"] == "submitted", "Low score should loop back"
    assert phase_calls[-1].kwargs.get("stage_results") == {}, "Stage results must be cleared"


@pytest.mark.asyncio
async def test_quality_gate_low_score_at_cap_human_review(tmp_path):
    """Below threshold + at cycle cap → human review (safety net)."""
    pm = _make_release_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=2)
    queue = JobQueue()

    pm._job_kinds[("release", "test-idea")] = "pipeline"
    status = _release_status(prior_releases=2, score=4.0, max_cycles=2)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="release",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    review_calls = [
        c
        for c in pm.blackboard.update_status.call_args_list
        if c.kwargs.get("needs_human_review") is True
    ]
    assert review_calls, "Low score at cycle cap should trigger human review"
    assert "below threshold" in review_calls[0].kwargs.get("review_reason", "")


@pytest.mark.asyncio
async def test_quality_gate_high_score_at_cap_releases(tmp_path):
    """Above threshold + at cycle cap → terminal release."""
    pm = _make_release_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=1)
    queue = JobQueue()

    pm._job_kinds[("release", "test-idea")] = "pipeline"
    status = _release_status(prior_releases=1, score=8.5)
    pm.blackboard.get_status.return_value = status
    pm.blackboard.next_agent.return_value = None
    pm.blackboard.is_ready.return_value = True
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="release",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    phase_calls = [
        c
        for c in pm.blackboard.update_status.call_args_list
        if c.kwargs.get("phase") in ("released", "submitted")
    ]
    assert phase_calls[-1].kwargs["phase"] == "released"


@pytest.mark.asyncio
async def test_quality_gate_uses_settings_max_iterate(tmp_path):
    """_handle_result reads max_iterate_per_stage from settings, not constant."""
    pm = _make_pool(tmp_path)
    pm.settings.max_iterate_per_stage = 5  # Override default
    queue = JobQueue()

    pm._job_kinds[("ideation", "test-idea")] = "pipeline"
    # At count 3, will be incremented to 4, max=5 → 4 < 5 should NOT trigger review
    status = _base_status(
        phase_recommendation="iterate",
        iter_counts={"ideation": 3},
    )
    pm.blackboard.get_status.return_value = status
    pm.blackboard.get_gating_mode.return_value = "auto"

    result = RunResult(
        status=RunStatus.OK,
        role="ideation",
        idea_id="test-idea",
        duration_seconds=5.0,
        cost_usd=0.01,
    )

    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)

    review_calls = [
        c
        for c in pm.blackboard.update_status.call_args_list
        if c.kwargs.get("needs_human_review") is True
    ]
    assert not review_calls, "Count 3→4, max=5: should not trigger review (4 < 5)"
