"""End-to-end and regression tests for settings persistence, quality gate, and feedback UX.

Covers:
1. Config overlay: load, save, merge, cache invalidation, edge cases
2. Quality gate decision matrix: all 4 branches + disabled mode
3. Settings route: POST/GET round-trip, validation clamping, saved banner
4. Idea route regressions: dismiss_review, serviced_agents in context
5. _handle_release regressions: unlimited refinement, per-idea overrides, post_ready clearing
6. Template rendering: settings page sections, idea detail feedback variants
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from trellis.config import (
    Settings,
    _invalidate_settings_cache,
    _load_project_settings,
    get_settings,
    save_project_settings,
)
from trellis.orchestrator.job_queue import JobQueue
from trellis.orchestrator.pool import MAX_ITERATE_PER_STAGE, PoolManager
from trellis.orchestrator.worker import RunResult, RunStatus


# ── Helpers ──────────────────────────────────────────────────────────


def _make_pool(
    tmp_path, *, min_quality_score=0.0, max_refinement_cycles=1, max_iterate_per_stage=3
):
    """Build a PoolManager with explicit settings for quality gate tests."""
    pm = PoolManager.__new__(PoolManager)
    pm.settings = MagicMock(
        pool_size=2,
        job_timeout_minutes=60,
        producer_interval_seconds=10,
        project_root=tmp_path,
        telegram_bot_token="test",
        telegram_chat_id="test",
        max_iterate_per_stage=max_iterate_per_stage,
        max_refinement_cycles=max_refinement_cycles,
        min_quality_score=min_quality_score,
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
    pm.blackboard.get_pipeline.return_value = {
        "agents": ["ideation", "implementation", "release"],
        "post_ready": ["watcher"],
    }
    return pm


def _release_status(prior_releases=0, score=5.0, max_cycles=None, with_post_ready=False):
    """Build a status dict ready for _handle_release."""
    history = [
        {"from": "release", "to": "released", "at": "2026-03-13T00:00:00Z"}
        for _ in range(prior_releases)
    ]
    serviced = {}
    if with_post_ready:
        serviced = {"ideation": "2026-03-01T00:00:00Z", "watcher": "2026-03-01T00:00:00Z"}
    status = {
        "id": "test-idea",
        "phase": "release",
        "phase_history": history,
        "last_serviced_by": serviced,
        "total_cost_usd": 0.0,
        "iter_counts": {},
        "iteration_count": 0,
        "stage_results": {"ideation": "proceed", "implementation": "proceed"},
        "phase_recommendation": "proceed",
        "deadline_hits": {},
        "priority_score": score,
    }
    if max_cycles is not None:
        status["max_refinement_cycles"] = max_cycles
    return status


def _pipeline_status(role="ideation", recommendation="iterate", iter_counts=None):
    """Build a status dict for _handle_result iterate/proceed tests."""
    return {
        "id": "test-idea",
        "phase": role,
        "phase_history": [],
        "last_serviced_by": {},
        "total_cost_usd": 0.0,
        "iter_counts": iter_counts or {},
        "iteration_count": sum((iter_counts or {}).values()),
        "stage_results": {},
        "phase_recommendation": recommendation,
        "deadline_hits": {},
        "priority_score": 5.0,
    }


async def _run_handle_result(pm, result, queue=None):
    """Run _handle_result with broadcast mocked."""
    queue = queue or JobQueue()
    with patch("trellis.orchestrator.pool.PoolManager._broadcast_sync"):
        await pm._handle_result(result, queue)
    return pm.blackboard.update_status.call_args_list


def _find_phase_call(calls, phase_value):
    """Find the update_status call that sets phase to a specific value."""
    return [c for c in calls if c.kwargs.get("phase") == phase_value]


def _find_review_call(calls):
    """Find the update_status call that sets needs_human_review=True."""
    return [c for c in calls if c.kwargs.get("needs_human_review") is True]


# ── 1. Config overlay ────────────────────────────────────────────────


@pytest.mark.integration
class TestConfigOverlay:
    def test_load_missing_file(self, tmp_path):
        assert _load_project_settings(tmp_path) == {}

    def test_load_invalid_json(self, tmp_path):
        (tmp_path / "project_settings.json").write_text("not json {{{")
        assert _load_project_settings(tmp_path) == {}

    def test_load_non_dict(self, tmp_path):
        (tmp_path / "project_settings.json").write_text("[1,2,3]")
        assert _load_project_settings(tmp_path) == {}

    def test_load_valid_overlay(self, tmp_path):
        (tmp_path / "project_settings.json").write_text('{"pool_size": 5}')
        assert _load_project_settings(tmp_path) == {"pool_size": 5}

    def test_save_creates_file(self, tmp_path):
        with patch("trellis.config._discover_project_root", return_value=tmp_path):
            save_project_settings({"min_quality_score": 7.0})
        path = tmp_path / "project_settings.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["min_quality_score"] == 7.0

    def test_save_overwrites_existing(self, tmp_path):
        (tmp_path / "project_settings.json").write_text('{"old": true}')
        with patch("trellis.config._discover_project_root", return_value=tmp_path):
            save_project_settings({"new": True})
        data = json.loads((tmp_path / "project_settings.json").read_text())
        assert "old" not in data
        assert data["new"] is True

    def test_cache_invalidation(self, tmp_path):
        """After _invalidate_settings_cache, get_settings returns fresh data."""
        with patch("trellis.config._discover_project_root", return_value=tmp_path):
            _invalidate_settings_cache()
            s1 = get_settings()
            assert s1.min_quality_score == 0.0

            save_project_settings({"min_quality_score": 8.0})
            _invalidate_settings_cache()
            s2 = get_settings()
            assert s2.min_quality_score == 8.0

            # Clean up
            (tmp_path / "project_settings.json").unlink(missing_ok=True)
            _invalidate_settings_cache()

    def test_overlay_unknown_fields_dont_crash(self, tmp_path):
        """Unknown fields in overlay don't prevent loading valid settings."""
        (tmp_path / "project_settings.json").write_text(
            '{"totally_unknown_field": 999, "pool_size": 7}'
        )
        with patch("trellis.config._discover_project_root", return_value=tmp_path):
            _invalidate_settings_cache()
            s = get_settings()
            # Known fields still work
            assert s.pool_size == 7
            assert s.max_iterate_per_stage == 3  # default preserved
            (tmp_path / "project_settings.json").unlink(missing_ok=True)
            _invalidate_settings_cache()

    def test_overlay_preserves_base_when_empty(self, tmp_path):
        """An empty overlay file doesn't change defaults."""
        (tmp_path / "project_settings.json").write_text("{}")
        with patch("trellis.config._discover_project_root", return_value=tmp_path):
            _invalidate_settings_cache()
            s = get_settings()
            assert s.pool_size == 3
            assert s.min_quality_score == 0.0
            (tmp_path / "project_settings.json").unlink(missing_ok=True)
            _invalidate_settings_cache()

    def test_new_settings_defaults(self):
        """New Settings fields have correct defaults matching the old behavior."""
        s = Settings()
        assert s.max_iterate_per_stage == 3
        assert s.max_iterate_per_stage == MAX_ITERATE_PER_STAGE  # must match old constant
        assert s.max_refinement_cycles == 1
        assert s.min_quality_score == 0.0


# ── 2. Quality gate decision matrix ─────────────────────────────────


@pytest.mark.unit
class TestQualityGateMatrix:
    """Tests the 4-case decision matrix in _handle_release:
    | Quality OK? | At cap? | Action          |
    |-------------|---------|-----------------|
    | Yes         | Yes     | Terminal release |
    | Yes         | No      | Loop back       |
    | No          | No      | Loop back       |
    | No          | Yes     | Human review    |
    """

    @pytest.mark.asyncio
    async def test_quality_ok_at_cap_releases(self, tmp_path):
        """Quality OK + at cycle cap → terminal 'released'."""
        pm = _make_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=2)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=2, score=8.0, max_cycles=2)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "released"), "Quality OK + at cap → terminal release"
        assert not _find_phase_call(calls, "submitted"), "Should NOT loop back"
        assert not _find_review_call(calls), "Should NOT trigger human review"

    @pytest.mark.asyncio
    async def test_quality_ok_under_cap_loops(self, tmp_path):
        """Quality OK + under cycle cap → loop back for normal refinement."""
        pm = _make_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=3)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=1, score=8.0, max_cycles=3)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "submitted"), "Quality OK + under cap → loop back"
        loopback = _find_phase_call(calls, "submitted")[0]
        assert loopback.kwargs.get("stage_results") == {}, "stage_results must be cleared"

    @pytest.mark.asyncio
    async def test_quality_low_under_cap_loops(self, tmp_path):
        """Quality low + under cycle cap → loop back for quality-driven refinement."""
        pm = _make_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=3)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=0, score=3.0, max_cycles=3)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "submitted"), "Quality low + under cap → loop back"
        assert not _find_review_call(calls), "Should NOT go to human review yet"

    @pytest.mark.asyncio
    async def test_quality_low_at_cap_human_review(self, tmp_path):
        """Quality low + at cycle cap → human review (safety net)."""
        pm = _make_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=2)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=2, score=3.0, max_cycles=2)
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
        calls = await _run_handle_result(pm, result)

        review = _find_review_call(calls)
        assert review, "Quality low + at cap → human review"
        assert "below threshold" in review[0].kwargs["review_reason"]
        assert "3.0" in review[0].kwargs["review_reason"]
        assert "7.0" in review[0].kwargs["review_reason"]
        assert not _find_phase_call(calls, "released"), "Should NOT terminal release"
        assert not _find_phase_call(calls, "submitted"), "Should NOT loop back"

    @pytest.mark.asyncio
    async def test_gate_disabled_ignores_low_score(self, tmp_path):
        """With min_quality_score=0 (disabled), low-score idea releases normally."""
        pm = _make_pool(tmp_path, min_quality_score=0.0, max_refinement_cycles=1)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=1, score=1.0)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "released"), "Gate disabled → normal release"
        assert not _find_review_call(calls)

    @pytest.mark.asyncio
    async def test_gate_exact_threshold_passes(self, tmp_path):
        """Score exactly at threshold passes the quality gate."""
        pm = _make_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=1)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=1, score=7.0)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "released"), "Score == threshold → pass"

    @pytest.mark.asyncio
    async def test_gate_just_below_threshold_fails(self, tmp_path):
        """Score just below threshold fails the quality gate."""
        pm = _make_pool(tmp_path, min_quality_score=7.0, max_refinement_cycles=1)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=1, score=6.9)
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
        calls = await _run_handle_result(pm, result)

        review = _find_review_call(calls)
        assert review, "Score 6.9 < 7.0 → human review at cap"

    @pytest.mark.asyncio
    async def test_gate_missing_score_treated_as_zero(self, tmp_path):
        """Missing priority_score defaults to 0, which fails a non-zero gate."""
        pm = _make_pool(tmp_path, min_quality_score=5.0, max_refinement_cycles=1)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=1, score=5.0)
        del status["priority_score"]  # simulate missing field
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
        calls = await _run_handle_result(pm, result)

        review = _find_review_call(calls)
        assert review, "Missing score (→ 0) should fail gate of 5.0"


# ── 3. _handle_release regressions (behavior preserved from before) ──


@pytest.mark.regression
class TestReleaseRegressions:
    @pytest.mark.asyncio
    async def test_unlimited_refinement_always_loops(self, tmp_path):
        """max_refinement_cycles=0 means unlimited — always loop back, even at high count."""
        pm = _make_pool(tmp_path, min_quality_score=0.0, max_refinement_cycles=1)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        # Per-idea override: 0 = unlimited
        status = _release_status(prior_releases=50, score=8.0, max_cycles=0)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "submitted"), "Unlimited refinement must always loop back"
        assert not _find_phase_call(calls, "released")

    @pytest.mark.asyncio
    async def test_per_idea_max_cycles_overrides_settings(self, tmp_path):
        """Per-idea max_refinement_cycles in status.json takes precedence over settings."""
        pm = _make_pool(tmp_path, min_quality_score=0.0, max_refinement_cycles=1)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        # Settings says 1, per-idea says 5 → should still loop at prior_releases=2
        status = _release_status(prior_releases=2, score=8.0, max_cycles=5)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "submitted"), "Per-idea override should allow more cycles"

    @pytest.mark.asyncio
    async def test_settings_default_used_when_status_has_no_override(self, tmp_path):
        """When status has no max_refinement_cycles, settings default is used."""
        pm = _make_pool(tmp_path, min_quality_score=0.0, max_refinement_cycles=2)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        # No max_cycles in status → uses settings.max_refinement_cycles=2
        status = _release_status(prior_releases=1, score=8.0)
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
        calls = await _run_handle_result(pm, result)

        assert _find_phase_call(calls, "submitted"), "prior=1 < settings.max=2 → loop back"

    @pytest.mark.asyncio
    async def test_post_ready_cleared_on_loopback(self, tmp_path):
        """On loop-back, post_ready roles cleared from last_serviced_by (Bug 3 preserved)."""
        pm = _make_pool(tmp_path, min_quality_score=0.0, max_refinement_cycles=3)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=0, score=8.0, max_cycles=3, with_post_ready=True)
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
        calls = await _run_handle_result(pm, result)

        loopback = _find_phase_call(calls, "submitted")
        assert loopback
        serviced = loopback[0].kwargs.get("last_serviced_by", {})
        assert "watcher" not in serviced, "post_ready role 'watcher' must be cleared"

    @pytest.mark.asyncio
    async def test_phase_history_appended_on_all_paths(self, tmp_path):
        """phase_history gets a 'released' entry on every path (terminal, loopback, review)."""
        for scenario, kwargs, expected_review in [
            ("terminal", dict(prior_releases=1, score=8.0, max_cycles=1), False),
            ("loopback", dict(prior_releases=0, score=8.0, max_cycles=2), False),
            ("review", dict(prior_releases=2, score=3.0, max_cycles=2), True),
        ]:
            pm = _make_pool(
                tmp_path,
                min_quality_score=7.0 if expected_review else 0.0,
                max_refinement_cycles=kwargs["max_cycles"],
            )
            pm._job_kinds[("release", "test-idea")] = "pipeline"
            status = _release_status(**kwargs)
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
            calls = await _run_handle_result(pm, result)

            # Find the call with phase_history
            history_calls = [c for c in calls if "phase_history" in c.kwargs]
            assert history_calls, f"[{scenario}] phase_history must be passed"
            history = history_calls[-1].kwargs["phase_history"]
            released_entries = [e for e in history if e.get("to") == "released"]
            assert len(released_entries) >= kwargs["prior_releases"] + 1, (
                f"[{scenario}] Must append a new 'released' entry"
            )

    @pytest.mark.asyncio
    async def test_stage_results_not_cleared_on_terminal(self, tmp_path):
        """Terminal release must NOT pass stage_results={} — preserves final record."""
        pm = _make_pool(tmp_path, min_quality_score=0.0, max_refinement_cycles=1)
        pm._job_kinds[("release", "test-idea")] = "pipeline"
        status = _release_status(prior_releases=1, score=8.0)
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
        calls = await _run_handle_result(pm, result)

        terminal = _find_phase_call(calls, "released")
        assert terminal
        assert "stage_results" not in terminal[0].kwargs, "Terminal must not clear stage_results"


# ── 4. _handle_result iterate cap regressions ────────────────────────


@pytest.mark.regression
class TestIterateCapRegressions:
    @pytest.mark.asyncio
    async def test_settings_max_iterate_replaces_constant(self, tmp_path):
        """max_iterate_per_stage from settings is used, not the module constant."""
        pm = _make_pool(tmp_path, max_iterate_per_stage=10)
        pm._job_kinds[("ideation", "test-idea")] = "pipeline"
        # iter_counts will go from 8→9, which is < 10 → should NOT trigger review
        status = _pipeline_status(iter_counts={"ideation": 8}, recommendation="iterate")
        pm.blackboard.get_status.return_value = status
        pm.blackboard.get_gating_mode.return_value = "auto"

        result = RunResult(
            status=RunStatus.OK,
            role="ideation",
            idea_id="test-idea",
            duration_seconds=5.0,
            cost_usd=0.01,
        )
        calls = await _run_handle_result(pm, result)

        assert not _find_review_call(calls), "8→9 < 10 → no review"

    @pytest.mark.asyncio
    async def test_settings_max_iterate_triggers_at_threshold(self, tmp_path):
        """At exactly the settings cap, review IS triggered."""
        pm = _make_pool(tmp_path, max_iterate_per_stage=5)
        pm._job_kinds[("ideation", "test-idea")] = "pipeline"
        # iter_counts will go from 4→5, which >= 5 → SHOULD trigger review
        status = _pipeline_status(iter_counts={"ideation": 4}, recommendation="iterate")
        pm.blackboard.get_status.return_value = status
        pm.blackboard.get_gating_mode.return_value = "auto"

        result = RunResult(
            status=RunStatus.OK,
            role="ideation",
            idea_id="test-idea",
            duration_seconds=5.0,
            cost_usd=0.01,
        )
        calls = await _run_handle_result(pm, result)

        review = _find_review_call(calls)
        assert review, "4→5 >= 5 → review"
        assert "5" in review[0].kwargs["review_reason"]

    @pytest.mark.asyncio
    async def test_default_max_iterate_matches_old_constant(self, tmp_path):
        """Default max_iterate_per_stage=3 produces same behavior as old MAX_ITERATE_PER_STAGE=3."""
        pm = _make_pool(tmp_path)  # default max_iterate_per_stage=3
        pm._job_kinds[("ideation", "test-idea")] = "pipeline"
        status = _pipeline_status(iter_counts={"ideation": 2}, recommendation="iterate")
        pm.blackboard.get_status.return_value = status
        pm.blackboard.get_gating_mode.return_value = "auto"

        result = RunResult(
            status=RunStatus.OK,
            role="ideation",
            idea_id="test-idea",
            duration_seconds=5.0,
            cost_usd=0.01,
        )
        calls = await _run_handle_result(pm, result)

        review = _find_review_call(calls)
        assert review, "2→3 >= 3 → review (matches old constant)"


# ── 5. Settings route e2e ────────────────────────────────────────────


@pytest.mark.e2e
class TestSettingsRoutes:
    @pytest.fixture
    def app(self):
        from trellis.web.api.app import create_app

        return create_app()

    @pytest.mark.asyncio
    async def test_settings_page_renders_all_sections(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/settings/")
        assert resp.status_code == 200
        body = resp.text
        assert "Pool" in body
        assert "Quality Gate" in body
        assert "Budget Limits" in body
        assert "Model Tiers" in body
        assert "min_quality_score" in body
        assert "max_iterate_per_stage" in body
        assert "max_refinement_cycles" in body
        assert "restart required" in body

    @pytest.mark.asyncio
    async def test_settings_page_saved_banner(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/settings/?saved=system")
        assert resp.status_code == 200
        assert "System settings saved" in resp.text

    @pytest.mark.asyncio
    async def test_settings_page_global_saved_banner(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/settings/?saved=global")
        assert resp.status_code == 200
        assert "Global prompt updated" in resp.text

    @pytest.mark.asyncio
    async def test_api_settings_returns_all_fields(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/settings/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {
            "pool_size",
            "job_timeout_minutes",
            "producer_interval_seconds",
            "max_iterate_per_stage",
            "max_refinement_cycles",
            "min_quality_score",
            "max_budget_ideation",
            "max_budget_implementation",
            "max_budget_validation",
            "max_budget_release",
            "max_budget_watcher",
            "model_tier_high",
            "model_tier_low",
        }
        assert expected_keys <= set(data.keys()), (
            f"Missing keys: {expected_keys - set(data.keys())}"
        )

    @pytest.mark.asyncio
    async def test_post_system_saves_and_redirects(self, app, tmp_path):
        """POST /settings/system saves to project_settings.json and redirects."""
        with patch("trellis.config._discover_project_root", return_value=tmp_path):
            _invalidate_settings_cache()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/settings/system",
                    data={
                        "pool_size": "4",
                        "job_timeout_minutes": "30",
                        "producer_interval_seconds": "5",
                        "max_iterate_per_stage": "7",
                        "max_refinement_cycles": "3",
                        "min_quality_score": "6.5",
                        "max_budget_ideation": "1.00",
                        "max_budget_implementation": "15.00",
                        "max_budget_validation": "2.00",
                        "max_budget_release": "5.00",
                        "max_budget_watcher": "0.20",
                        "model_tier_high": "claude-opus-4-6",
                        "model_tier_low": "claude-haiku-4-5",
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 303
            assert resp.headers["location"] == "/settings?saved=system"

            # Verify file was written
            path = tmp_path / "project_settings.json"
            assert path.exists()
            data = json.loads(path.read_text())
            assert data["max_iterate_per_stage"] == 7
            assert data["min_quality_score"] == 6.5
            assert data["model_tier_high"] == "claude-opus-4-6"

            # Verify API reflects new values
            _invalidate_settings_cache()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/settings/api/settings")
            api_data = resp.json()
            assert api_data["max_iterate_per_stage"] == 7
            assert api_data["min_quality_score"] == 6.5

            # Clean up
            path.unlink(missing_ok=True)
            _invalidate_settings_cache()

    @pytest.mark.asyncio
    async def test_post_validation_clamps_values(self, app, tmp_path):
        """POST with out-of-range values gets clamped."""
        with patch("trellis.config._discover_project_root", return_value=tmp_path):
            _invalidate_settings_cache()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/settings/system",
                    data={
                        "pool_size": "999",  # max 20
                        "job_timeout_minutes": "0",  # min 1
                        "producer_interval_seconds": "5",
                        "max_iterate_per_stage": "-5",  # min 1
                        "max_refinement_cycles": "-1",  # min 0
                        "min_quality_score": "15.0",  # max 10
                        "max_budget_ideation": "-1",  # min 0
                        "max_budget_implementation": "10",
                        "max_budget_validation": "1",
                        "max_budget_release": "3",
                        "max_budget_watcher": "0.1",
                        "model_tier_high": "test",
                        "model_tier_low": "test",
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 303

            data = json.loads((tmp_path / "project_settings.json").read_text())
            assert data["pool_size"] == 20, "pool_size clamped to max 20"
            assert data["job_timeout_minutes"] == 1, "timeout clamped to min 1"
            assert data["max_iterate_per_stage"] == 1, "max_iterate clamped to min 1"
            assert data["max_refinement_cycles"] == 0, "refinement clamped to min 0"
            assert data["min_quality_score"] == 10.0, "quality clamped to max 10"
            assert data["max_budget_ideation"] == 0.0, "budget clamped to min 0"

            # Clean up
            (tmp_path / "project_settings.json").unlink(missing_ok=True)
            _invalidate_settings_cache()

    @pytest.mark.asyncio
    async def test_global_prompt_save_still_works(self, app, tmp_path):
        """Regression: global prompt save route still functions."""
        mock_settings = MagicMock()
        mock_settings.project_root = tmp_path
        with patch("trellis.web.api.routes.settings.get_settings", return_value=mock_settings):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/settings/global-prompt",
                    data={"global_prompt": "Test prompt content"},
                    follow_redirects=False,
                )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings?saved=global"
        assert (tmp_path / "global-system-prompt.md").read_text() == "Test prompt content"


# ── 6. Ideas route regressions ───────────────────────────────────────


@pytest.mark.regression
class TestIdeasRouteRegressions:
    @pytest.mark.asyncio
    async def test_dismiss_review_uses_settings(self):
        """dismiss_review reads max_iterate_per_stage from settings, not constant."""
        from trellis.web.api.routes.ideas import idea_action

        bb = MagicMock()
        bb.get_status.return_value = {
            "iter_counts": {"ideation": 5, "validation": 1},
        }
        mock_settings = MagicMock(max_iterate_per_stage=5)

        with (
            patch("trellis.web.api.routes.ideas._get_blackboard", return_value=bb),
            patch("trellis.web.api.routes.ideas.get_settings", return_value=mock_settings),
        ):
            await idea_action(idea_id="test", action="dismiss_review")

        call_kwargs = bb.update_status.call_args.kwargs
        assert call_kwargs["iter_counts"]["ideation"] == 0, "5 >= 5 → reset"
        assert call_kwargs["iter_counts"]["validation"] == 1, "1 < 5 → keep"

    @pytest.mark.asyncio
    async def test_dismiss_review_with_higher_cap(self):
        """With max_iterate=10, an agent at count 5 should NOT be reset."""
        from trellis.web.api.routes.ideas import idea_action

        bb = MagicMock()
        bb.get_status.return_value = {
            "iter_counts": {"ideation": 5, "validation": 10},
        }
        mock_settings = MagicMock(max_iterate_per_stage=10)

        with (
            patch("trellis.web.api.routes.ideas._get_blackboard", return_value=bb),
            patch("trellis.web.api.routes.ideas.get_settings", return_value=mock_settings),
        ):
            await idea_action(idea_id="test", action="dismiss_review")

        call_kwargs = bb.update_status.call_args.kwargs
        assert call_kwargs["iter_counts"]["ideation"] == 5, "5 < 10 → keep"
        assert call_kwargs["iter_counts"]["validation"] == 0, "10 >= 10 → reset"

    @pytest.mark.asyncio
    async def test_idea_detail_passes_serviced_agents(self):
        """idea_detail route includes serviced_agents in template context."""
        from trellis.web.api.routes.ideas import idea_detail

        bb = MagicMock()
        idea_dir = MagicMock()
        idea_dir.iterdir.return_value = []
        bb.idea_dir.return_value = idea_dir
        bb.get_status.return_value = {
            "id": "test",
            "title": "Test",
            "phase": "ideation",
            "phase_history": [],
            "last_serviced_by": {
                "ideation": "2026-03-01T00:00:00Z",
                "validation": "2026-03-01T00:00:00Z",
            },
        }
        bb.get_pipeline.return_value = {"agents": [], "post_ready": []}
        bb.list_ideas.return_value = ["test"]

        mock_request = MagicMock()
        mock_settings = MagicMock()
        mock_settings.project_root = Path("/fake")
        mock_settings.blackboard_dir = Path("/fake/bb")
        mock_settings.registry_path = Path("/fake/registry.yaml")

        mock_template_resp = MagicMock()
        with (
            patch("trellis.web.api.routes.ideas._get_blackboard", return_value=bb),
            patch("trellis.web.api.routes.ideas.get_settings", return_value=mock_settings),
            patch("trellis.web.api.routes.ideas._get_registered_roles", return_value=set()),
            patch("trellis.web.api.routes.ideas._load_feedback", return_value=[]),
            patch("trellis.web.api.routes.ideas._load_questions", return_value=[]),
            patch("trellis.web.api.routes.ideas.templates") as mock_templates,
        ):
            mock_templates.TemplateResponse.return_value = mock_template_resp
            await idea_detail(mock_request, "test")

        ctx = (
            mock_templates.TemplateResponse.call_args[1]
            if mock_templates.TemplateResponse.call_args[1]
            else mock_templates.TemplateResponse.call_args[0][1]
        )
        assert "serviced_agents" in ctx
        assert ctx["serviced_agents"] == ["ideation", "validation"]


# ── 7. Template rendering checks ────────────────────────────────────


@pytest.mark.smoke
class TestTemplateRendering:
    """Parse templates to verify structural correctness."""

    def test_settings_form_field_names(self):
        """Settings form has all required field names."""
        path = (
            Path(__file__).resolve().parent.parent
            / "trellis"
            / "web"
            / "frontend"
            / "templates"
            / "settings.html"
        )
        content = path.read_text()
        required_fields = [
            'name="pool_size"',
            'name="job_timeout_minutes"',
            'name="producer_interval_seconds"',
            'name="max_iterate_per_stage"',
            'name="max_refinement_cycles"',
            'name="min_quality_score"',
            'name="max_budget_ideation"',
            'name="max_budget_implementation"',
            'name="max_budget_validation"',
            'name="max_budget_release"',
            'name="max_budget_watcher"',
            'name="model_tier_high"',
            'name="model_tier_low"',
        ]
        for field in required_fields:
            assert field in content, f"Missing form field: {field}"

    def test_settings_form_action(self):
        """Settings form POSTs to /settings/system."""
        path = (
            Path(__file__).resolve().parent.parent
            / "trellis"
            / "web"
            / "frontend"
            / "templates"
            / "settings.html"
        )
        content = path.read_text()
        assert 'action="/settings/system"' in content

    def test_idea_detail_feedback_empty_state(self):
        """idea_detail.html has feedback empty state for when no feedback exists."""
        path = (
            Path(__file__).resolve().parent.parent
            / "trellis"
            / "web"
            / "frontend"
            / "templates"
            / "idea_detail.html"
        )
        content = path.read_text()
        assert "No feedback yet" in content
        assert "Select any text in an artifact" in content
        assert "+ Give Feedback" in content

    def test_idea_detail_feedback_agent_chips(self):
        """idea_detail.html has agent chips section in feedback modal."""
        path = (
            Path(__file__).resolve().parent.parent
            / "trellis"
            / "web"
            / "frontend"
            / "templates"
            / "idea_detail.html"
        )
        content = path.read_text()
        assert "will process this feedback" in content
        assert "queued for the first agent" in content
        assert "serviced_agents" in content

    def test_idea_detail_feedback_button_always_visible(self):
        """Per-artifact feedback buttons are always visible (no opacity-0 group-hover)."""
        path = (
            Path(__file__).resolve().parent.parent
            / "trellis"
            / "web"
            / "frontend"
            / "templates"
            / "idea_detail.html"
        )
        content = path.read_text()
        # The old pattern was: opacity-0 group-hover/art:opacity-100
        # This should NOT be present on the feedback button anymore
        # Find the feedback button line specifically
        import re

        feedback_btn_match = re.search(
            r'onclick.*feedbackState\.artifact.*\n\s+class="([^"]*)">\s*feedback', content
        )
        assert feedback_btn_match, "Feedback button should exist on artifact summaries"
        btn_classes = feedback_btn_match.group(1)
        assert "opacity-0" not in btn_classes, "Feedback button should always be visible"

    def test_idea_detail_toast_instead_of_reload(self):
        """submitFeedback uses toast + DOM update instead of window.location.reload."""
        import re

        path = (
            Path(__file__).resolve().parent.parent
            / "trellis"
            / "web"
            / "frontend"
            / "templates"
            / "idea_detail.html"
        )
        content = path.read_text()
        # Extract the submitFeedback function body
        match = re.search(
            r"window\.submitFeedback\s*=\s*async\s+function\(\)\s*\{(.+?)\n\s{4}\};",
            content,
            re.DOTALL,
        )
        assert match, "submitFeedback function should exist"
        fn_body = match.group(1)
        assert "window.location.reload" not in fn_body, (
            "submitFeedback should use toast, not reload"
        )
        assert "showToast" in fn_body, "submitFeedback should show toast"
        assert "Feedback submitted" in fn_body, "Toast message should mention feedback"
        assert "list.prepend" in fn_body, "Should dynamically prepend feedback chip"

    def test_idea_detail_jinja_syntax_valid(self):
        """idea_detail.html parses without Jinja syntax errors."""
        from jinja2 import Environment, FileSystemLoader

        tmpl_dir = str(
            Path(__file__).resolve().parent.parent / "trellis" / "web" / "frontend" / "templates"
        )
        env = Environment(loader=FileSystemLoader(tmpl_dir))
        env.parse(env.loader.get_source(env, "idea_detail.html")[0])

    def test_settings_jinja_syntax_valid(self):
        """settings.html parses without Jinja syntax errors."""
        from jinja2 import Environment, FileSystemLoader

        tmpl_dir = str(
            Path(__file__).resolve().parent.parent / "trellis" / "web" / "frontend" / "templates"
        )
        env = Environment(loader=FileSystemLoader(tmpl_dir))
        env.parse(env.loader.get_source(env, "settings.html")[0])
