"""Tests for pipeline + idea-status migration between old and new schemas."""

from __future__ import annotations

from trellis.core.pipeline import (
    DEFAULT_STAGE_NAME,
    LEGACY_AGENTS,
    LEGACY_WATCHERS,
    backfill_stage_fields,
    default_pipeline,
    initial_stage_fields,
    migrate_pipeline,
)


class TestDefaultPipeline:
    def test_has_both_legacy_and_stages_keys(self):
        p = default_pipeline()
        # Legacy keys (so old consumers keep working)
        assert p["agents"] == LEGACY_AGENTS
        assert p["post_ready"] == LEGACY_WATCHERS
        assert "parallel_groups" in p
        # New keys (so new consumers can target them)
        assert "stages" in p
        assert len(p["stages"]) == 1
        assert p["stages"][0]["name"] == DEFAULT_STAGE_NAME

    def test_default_has_four_single_role_groups(self):
        p = default_pipeline()
        groups = p["stages"][0]["role_groups"]
        assert len(groups) == 4
        for group, expected in zip(groups, LEGACY_AGENTS):
            assert len(group) == 1
            assert group[0]["name"] == expected
            assert group[0]["handler"] == "agent"

    def test_default_watchers_scope_is_wildcard(self):
        p = default_pipeline()
        watchers = p["stages"][0]["watchers"]
        for w in watchers:
            assert w["scope"] == ["*"]
            assert w["handler"] == "agent"

    def test_instances_are_independent(self):
        a = default_pipeline()
        b = default_pipeline()
        a["agents"].append("mutated")
        assert b["agents"] == LEGACY_AGENTS


class TestMigratePipeline:
    def test_old_shape_gets_stages_synthesized(self):
        legacy = {
            "agents": ["ideation", "release"],
            "post_ready": ["watcher-a"],
            "parallel_groups": [["ideation", "release"], ["watcher-a"]],
            "gating": {"default": "auto", "overrides": {}},
        }
        migrated = migrate_pipeline(legacy)
        assert "stages" in migrated
        assert migrated["stages"][0]["name"] == DEFAULT_STAGE_NAME
        names = [r[0]["name"] for r in migrated["stages"][0]["role_groups"]]
        assert names == ["ideation", "release"]
        assert migrated["stages"][0]["watchers"][0]["name"] == "watcher-a"
        # Legacy keys preserved
        assert migrated["agents"] == ["ideation", "release"]

    def test_new_shape_back_fills_legacy_keys(self):
        new = {
            "name": "app-idea",
            "stages": [
                {
                    "name": "prototype",
                    "role_groups": [
                        [{"name": "ideation", "handler": "agent"}],
                        [{"name": "impl", "handler": "agent"}],
                    ],
                    "watchers": [{"name": "w1", "handler": "agent", "scope": ["ideation"]}],
                }
            ],
        }
        migrated = migrate_pipeline(new)
        assert migrated["agents"] == ["ideation", "impl"]
        assert migrated["post_ready"] == ["w1"]
        assert migrated["stages"] == new["stages"]

    def test_missing_parallel_groups_is_synthesized(self):
        legacy = {"agents": ["a", "b"], "post_ready": ["w"]}
        migrated = migrate_pipeline(legacy)
        assert migrated["parallel_groups"] == [["a", "b"], ["w"]]

    def test_empty_pipeline_returns_default(self):
        migrated = migrate_pipeline({})
        assert migrated["agents"] == LEGACY_AGENTS

    def test_idempotent(self):
        legacy = {"agents": ["ideation"], "post_ready": []}
        once = migrate_pipeline(legacy)
        twice = migrate_pipeline(once)
        assert once["stages"] == twice["stages"]
        assert once["agents"] == twice["agents"]

    def test_does_not_mutate_input(self):
        legacy = {"agents": ["a"], "post_ready": []}
        original = dict(legacy)
        migrate_pipeline(legacy)
        assert legacy == original


class TestInitialStageFields:
    def test_returns_current_stage_stage_history_role_state(self):
        pipeline = default_pipeline()
        fields = initial_stage_fields(pipeline)
        assert fields["current_stage"] == DEFAULT_STAGE_NAME
        assert len(fields["stage_history"]) == 1
        assert fields["stage_history"][0]["stage"] == DEFAULT_STAGE_NAME
        assert fields["stage_history"][0]["exited_at"] is None
        assert fields["role_state"] == {DEFAULT_STAGE_NAME: {}}

    def test_picks_first_stage_for_custom_pipeline(self):
        pipeline = {
            "stages": [
                {"name": "prototype", "role_groups": [], "watchers": []},
                {"name": "v1", "role_groups": [], "watchers": []},
            ]
        }
        fields = initial_stage_fields(pipeline)
        assert fields["current_stage"] == "prototype"


class TestBackfillStageFields:
    def test_idempotent_when_already_migrated(self):
        status = {
            "current_stage": "v1",
            "role_state": {"v1": {"deploy": {"state": "proceed"}}},
            "stage_history": [{"stage": "v1", "entered_at": "x", "exited_at": None}],
        }
        assert backfill_stage_fields(status) == status

    def test_backfills_from_legacy_phase_and_stage_results(self):
        status = {
            "id": "test",
            "phase": "ideation",
            "created_at": "2026-04-18T00:00:00+00:00",
            "stage_results": {"ideation": "proceed", "implementation": "iterate"},
            "last_serviced_by": {"ideation": {"at": "2026-04-18T00:05:00+00:00"}},
            "iter_counts": {"ideation": 1, "implementation": 2},
            "pipeline": default_pipeline(),
        }
        migrated = backfill_stage_fields(status)
        assert migrated["current_stage"] == DEFAULT_STAGE_NAME
        rs = migrated["role_state"][DEFAULT_STAGE_NAME]
        assert rs["ideation"]["state"] == "proceed"
        assert rs["ideation"]["iterations"] == 1
        assert rs["ideation"]["completed_at"] == "2026-04-18T00:05:00+00:00"
        assert rs["implementation"]["state"] == "iterate"
        assert rs["implementation"]["iterations"] == 2
        assert migrated["stage_history"][0]["stage"] == DEFAULT_STAGE_NAME
        assert migrated["stage_history"][0]["entered_at"] == "2026-04-18T00:00:00+00:00"

    def test_backfill_with_no_legacy_fields(self):
        status = {"id": "bare", "phase": "submitted", "pipeline": default_pipeline()}
        migrated = backfill_stage_fields(status)
        assert migrated["current_stage"] == DEFAULT_STAGE_NAME
        assert migrated["role_state"] == {DEFAULT_STAGE_NAME: {}}


class TestBlackboardIntegration:
    def test_create_idea_populates_new_fields(self, blackboard):
        idea_id = blackboard.create_idea("Test Idea", "description")
        status = blackboard.get_status(idea_id)
        assert status["current_stage"] == DEFAULT_STAGE_NAME
        assert status["role_state"] == {DEFAULT_STAGE_NAME: {}}
        assert len(status["stage_history"]) == 1

    def test_get_pipeline_returns_migrated_shape(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        pipeline = blackboard.get_pipeline(idea_id)
        assert "stages" in pipeline
        assert pipeline["agents"] == LEGACY_AGENTS  # legacy back-compat

    def test_legacy_status_is_migrated_on_read(self, blackboard, tmp_path):
        # Create an idea then rewrite status.json in legacy shape
        idea_id = blackboard.create_idea("Legacy", "body")
        legacy_status = {
            "id": idea_id,
            "title": "Legacy",
            "phase": "implementation",
            "created_at": "2026-04-18T00:00:00+00:00",
            "updated_at": "2026-04-18T00:00:00+00:00",
            "pipeline": {
                "agents": list(LEGACY_AGENTS),
                "post_ready": list(LEGACY_WATCHERS),
            },
            "stage_results": {"ideation": "proceed"},
            "last_serviced_by": {"ideation": {"at": "2026-04-18T00:01:00+00:00"}},
        }
        import json

        blackboard.write_file(idea_id, "status.json", json.dumps(legacy_status))

        status = blackboard.get_status(idea_id)
        assert status["current_stage"] == DEFAULT_STAGE_NAME
        assert status["role_state"][DEFAULT_STAGE_NAME]["ideation"]["state"] == "proceed"
