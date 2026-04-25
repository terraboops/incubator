"""Tests for the stage-aware scheduler primitives."""

from __future__ import annotations

import json


from trellis.core.pipeline import (
    DEFAULT_STAGE_NAME,
    DONE_STAGE,
    compute_stage_advancement,
    default_pipeline,
    eligible_roles,
    next_stage_name,
    set_role_state,
    stage_is_complete,
)


def _status_with_pipeline(pipeline: dict, current: str | None = None) -> dict:
    stages = pipeline.get("stages") or []
    stage = current or (stages[0]["name"] if stages else DEFAULT_STAGE_NAME)
    return {
        "id": "test",
        "pipeline": pipeline,
        "current_stage": stage,
        "role_state": {stage: {}},
        "stage_history": [{"stage": stage, "entered_at": "", "exited_at": None}],
    }


class TestEligibleRoles:
    def test_fresh_idea_returns_first_group(self):
        status = _status_with_pipeline(default_pipeline())
        # Default pipeline: 4 single-role groups, sequential. First = ideation.
        assert eligible_roles(status) == ["ideation"]

    def test_parallel_group_returns_all_members(self):
        pipeline = {
            "name": "p",
            "stages": [
                {
                    "name": "s1",
                    "role_groups": [
                        [
                            {"name": "a", "handler": "agent"},
                            {"name": "b", "handler": "agent"},
                        ],
                        [{"name": "c", "handler": "agent"}],
                    ],
                    "watchers": [],
                }
            ],
        }
        status = _status_with_pipeline(pipeline)
        assert sorted(eligible_roles(status)) == ["a", "b"]

    def test_completed_roles_excluded_from_parallel_group(self):
        pipeline = {
            "name": "p",
            "stages": [
                {
                    "name": "s1",
                    "role_groups": [
                        [
                            {"name": "a", "handler": "agent"},
                            {"name": "b", "handler": "agent"},
                        ],
                    ],
                    "watchers": [],
                }
            ],
        }
        status = _status_with_pipeline(pipeline)
        status["role_state"]["s1"]["a"] = {"state": "proceed", "iterations": 1}
        assert eligible_roles(status) == ["b"]

    def test_group_advances_to_next_after_all_proceed(self):
        status = _status_with_pipeline(default_pipeline())
        status["role_state"][DEFAULT_STAGE_NAME]["ideation"] = {
            "state": "proceed",
            "iterations": 1,
        }
        assert eligible_roles(status) == ["implementation"]

    def test_all_groups_complete_returns_empty(self):
        status = _status_with_pipeline(default_pipeline())
        for role in ("ideation", "implementation", "validation", "release"):
            status["role_state"][DEFAULT_STAGE_NAME][role] = {
                "state": "proceed",
                "iterations": 1,
            }
        assert eligible_roles(status) == []

    def test_done_stage_returns_empty(self):
        status = _status_with_pipeline(default_pipeline(), current=DONE_STAGE)
        assert eligible_roles(status) == []

    def test_iterate_state_still_eligible(self):
        status = _status_with_pipeline(default_pipeline())
        status["role_state"][DEFAULT_STAGE_NAME]["ideation"] = {
            "state": "iterate",
            "iterations": 1,
        }
        assert eligible_roles(status) == ["ideation"]


class TestStageIsComplete:
    def test_fresh_idea_not_complete(self):
        assert not stage_is_complete(_status_with_pipeline(default_pipeline()))

    def test_all_roles_proceed_complete(self):
        status = _status_with_pipeline(default_pipeline())
        for role in ("ideation", "implementation", "validation", "release"):
            status["role_state"][DEFAULT_STAGE_NAME][role] = {"state": "proceed"}
        assert stage_is_complete(status)

    def test_done_stage_is_complete(self):
        status = _status_with_pipeline(default_pipeline(), current=DONE_STAGE)
        assert stage_is_complete(status)


class TestNextStageName:
    def test_advances_through_multi_stage_pipeline(self):
        pipeline = {
            "stages": [
                {"name": "prototype", "role_groups": [], "watchers": []},
                {"name": "v1", "role_groups": [], "watchers": []},
                {"name": "v2", "role_groups": [], "watchers": []},
            ]
        }
        assert next_stage_name(pipeline, "prototype") == "v1"
        assert next_stage_name(pipeline, "v1") == "v2"
        assert next_stage_name(pipeline, "v2") == DONE_STAGE

    def test_unknown_current_returns_done(self):
        pipeline = {"stages": [{"name": "prototype", "role_groups": []}]}
        assert next_stage_name(pipeline, "nonexistent") == DONE_STAGE


class TestComputeStageAdvancement:
    def test_incomplete_stage_returns_none(self):
        status = _status_with_pipeline(default_pipeline())
        assert compute_stage_advancement(status) is None

    def test_single_stage_pipeline_advances_to_done(self):
        status = _status_with_pipeline(default_pipeline())
        for role in ("ideation", "implementation", "validation", "release"):
            status["role_state"][DEFAULT_STAGE_NAME][role] = {"state": "proceed"}

        result = compute_stage_advancement(status)

        assert result is not None
        assert result["current_stage"] == DONE_STAGE
        assert DONE_STAGE in result["role_state"]
        # stage_history: old stage gets exited_at, new stage appended with entered_at
        assert result["stage_history"][-1]["stage"] == DONE_STAGE
        assert result["stage_history"][-1]["exited_at"] is None
        # Previous stage's exited_at populated
        prior = [e for e in result["stage_history"] if e["stage"] == DEFAULT_STAGE_NAME]
        assert prior[0]["exited_at"] is not None

    def test_multi_stage_advances_to_next(self):
        pipeline = {
            "stages": [
                {
                    "name": "prototype",
                    "role_groups": [[{"name": "a", "handler": "agent"}]],
                    "watchers": [],
                },
                {
                    "name": "v1",
                    "role_groups": [[{"name": "b", "handler": "agent"}]],
                    "watchers": [],
                },
            ]
        }
        status = _status_with_pipeline(pipeline, current="prototype")
        status["role_state"]["prototype"]["a"] = {"state": "proceed"}

        result = compute_stage_advancement(status)

        assert result["current_stage"] == "v1"
        assert result["role_state"]["v1"] == {}  # fresh role_state for new stage

    def test_done_stage_does_not_re_advance(self):
        status = _status_with_pipeline(default_pipeline(), current=DONE_STAGE)
        assert compute_stage_advancement(status) is None


class TestSetRoleState:
    def test_first_call_sets_iterations_to_1(self):
        status = _status_with_pipeline(default_pipeline())
        new_state = set_role_state(status, "ideation", "iterate")
        assert new_state[DEFAULT_STAGE_NAME]["ideation"]["state"] == "iterate"
        assert new_state[DEFAULT_STAGE_NAME]["ideation"]["iterations"] == 1

    def test_subsequent_calls_increment_iterations(self):
        status = _status_with_pipeline(default_pipeline())
        status["role_state"][DEFAULT_STAGE_NAME]["ideation"] = {
            "state": "iterate",
            "iterations": 3,
        }
        new_state = set_role_state(status, "ideation", "proceed")
        assert new_state[DEFAULT_STAGE_NAME]["ideation"]["iterations"] == 4
        assert new_state[DEFAULT_STAGE_NAME]["ideation"]["state"] == "proceed"

    def test_explicit_iterations_override(self):
        status = _status_with_pipeline(default_pipeline())
        new_state = set_role_state(status, "ideation", "proceed", iterations=7)
        assert new_state[DEFAULT_STAGE_NAME]["ideation"]["iterations"] == 7

    def test_completed_at_recorded(self):
        status = _status_with_pipeline(default_pipeline())
        new_state = set_role_state(
            status, "ideation", "proceed", completed_at="2026-04-18T00:00:00+00:00"
        )
        assert (
            new_state[DEFAULT_STAGE_NAME]["ideation"]["completed_at"] == "2026-04-18T00:00:00+00:00"
        )

    def test_does_not_mutate_input(self):
        status = _status_with_pipeline(default_pipeline())
        original = json.loads(json.dumps(status))
        set_role_state(status, "ideation", "proceed")
        assert status == original


class TestBlackboardIntegration:
    def test_next_roles_returns_first_group(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        assert blackboard.next_roles_in_current_stage(idea_id) == ["ideation"]

    def test_mark_role_state_flips_and_advances_eligibility(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        blackboard.mark_role_state(idea_id, "ideation", "proceed")
        assert blackboard.next_roles_in_current_stage(idea_id) == ["implementation"]

    def test_advance_stage_returns_new_stage_when_complete(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        for role in ("ideation", "implementation", "validation", "release"):
            blackboard.mark_role_state(idea_id, role, "proceed")
        assert blackboard.advance_stage_if_complete(idea_id) == DONE_STAGE
        assert blackboard.is_done(idea_id)

    def test_advance_stage_returns_none_when_not_complete(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        assert blackboard.advance_stage_if_complete(idea_id) is None

    def test_is_done_defaults_to_false(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        assert not blackboard.is_done(idea_id)
