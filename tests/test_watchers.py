"""Tests for watcher scope + reactivation (custom-stages spec)."""

from __future__ import annotations

import pytest

from trellis.core.pipeline import (
    DEFAULT_STAGE_NAME,
    DONE_STAGE,
    apply_reactivation,
    default_pipeline,
    watcher_is_in_scope,
)


def _status(current: str = DEFAULT_STAGE_NAME, completed_roles: list[str] = None) -> dict:
    role_state: dict = {current: {}}
    for r in completed_roles or []:
        role_state[current][r] = {"state": "proceed"}
    return {
        "id": "test",
        "pipeline": default_pipeline(),
        "current_stage": current,
        "role_state": role_state,
        "stage_history": [{"stage": current, "entered_at": "", "exited_at": None}],
    }


class TestWatcherIsInScope:
    def test_wildcard_always_fires(self):
        assert watcher_is_in_scope({"scope": ["*"]}, _status())
        assert watcher_is_in_scope({"scope": ["*"]}, _status(current=DONE_STAGE))

    def test_no_scope_defaults_to_wildcard(self):
        # Back-compat: today's post_ready watchers have no scope field.
        assert watcher_is_in_scope({"name": "w"}, _status())

    def test_role_name_match(self):
        status = _status()  # first eligible = ideation
        assert watcher_is_in_scope({"scope": ["ideation"]}, status)

    def test_role_name_not_matching_when_advanced(self):
        status = _status(completed_roles=["ideation"])  # eligible = implementation
        assert not watcher_is_in_scope({"scope": ["ideation"]}, status)
        assert watcher_is_in_scope({"scope": ["implementation"]}, status)

    def test_multiple_role_names(self):
        status = _status()
        assert watcher_is_in_scope({"scope": ["ideation", "validation"]}, status)

    def test_done_scope_fires_only_in_done(self):
        assert watcher_is_in_scope({"scope": ["done"]}, _status(current=DONE_STAGE))
        assert not watcher_is_in_scope({"scope": ["done"]}, _status())


class TestApplyReactivation:
    def test_requires_to_stage(self):
        with pytest.raises(ValueError, match="to_stage"):
            apply_reactivation(_status(current=DONE_STAGE), {})

    def test_target_stage_must_exist_or_be_added(self):
        with pytest.raises(ValueError, match="not in pipeline"):
            apply_reactivation(
                _status(current=DONE_STAGE),
                {"to_stage": "hotfix"},
            )

    def test_reactivate_with_add_stage_appends_and_shifts(self):
        status = _status(current=DONE_STAGE)
        reactivate = {
            "to_stage": "hotfix",
            "add_stage": {
                "name": "hotfix",
                "role_groups": [[{"name": "fixer", "handler": "agent"}]],
            },
        }

        updates = apply_reactivation(status, reactivate, watcher_name="uptime-watcher")

        assert updates["current_stage"] == "hotfix"
        stages = updates["pipeline"]["stages"]
        assert any(s["name"] == "hotfix" for s in stages)
        hotfix_stage = next(s for s in stages if s["name"] == "hotfix")
        assert hotfix_stage["added_by"] == "uptime-watcher"
        assert "added_at" in hotfix_stage

        # Stage history appends a reactivation entry
        last = updates["stage_history"][-1]
        assert last["stage"] == "hotfix"
        assert last["reactivated_by"] == "uptime-watcher"
        assert last["exited_at"] is None

        # Fresh role_state for the new stage
        assert updates["role_state"]["hotfix"] == {}

    def test_reactivate_idempotent_for_add_stage(self):
        status = _status(current=DONE_STAGE)
        reactivate = {
            "to_stage": "hotfix",
            "add_stage": {
                "name": "hotfix",
                "role_groups": [[{"name": "fixer", "handler": "agent"}]],
            },
        }
        first = apply_reactivation(status, reactivate, watcher_name="w")
        status["pipeline"] = first["pipeline"]
        status["current_stage"] = first["current_stage"]

        # Second reactivation of the same add_stage doesn't double-insert.
        second = apply_reactivation(status, reactivate, watcher_name="w")
        hotfix_count = sum(1 for s in second["pipeline"]["stages"] if s.get("name") == "hotfix")
        assert hotfix_count == 1

    def test_reactivate_to_existing_stage_without_add_stage(self):
        # If hotfix already exists, reactivation can target it without add_stage.
        status = _status(current=DONE_STAGE)
        # Pre-populate with a hotfix stage
        status["pipeline"]["stages"].append(
            {
                "name": "hotfix",
                "role_groups": [[{"name": "fixer", "handler": "agent"}]],
                "watchers": [],
            }
        )
        updates = apply_reactivation(status, {"to_stage": "hotfix"}, watcher_name="w")
        assert updates["current_stage"] == "hotfix"


class TestBlackboardIntegration:
    def test_reactivate_from_done_end_to_end(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        # Drive to done
        for role in ("ideation", "implementation", "validation", "release"):
            blackboard.mark_role_state(idea_id, role, "proceed")
        blackboard.advance_stage_if_complete(idea_id)
        assert blackboard.is_done(idea_id)

        reactivate = {
            "to_stage": "hotfix",
            "add_stage": {
                "name": "hotfix",
                "role_groups": [[{"name": "fixer", "handler": "agent"}]],
            },
        }
        new_stage = blackboard.reactivate_from_done(
            idea_id, reactivate, watcher_name="uptime-watcher"
        )
        assert new_stage == "hotfix"
        assert blackboard.get_status(idea_id)["current_stage"] == "hotfix"
        assert blackboard.next_roles_in_current_stage(idea_id) == ["fixer"]

    def test_watcher_scope_check_wired_on_blackboard(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        assert blackboard.watcher_is_in_scope(idea_id, {"scope": ["ideation"]})
        assert not blackboard.watcher_is_in_scope(idea_id, {"scope": ["done"]})
