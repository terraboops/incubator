"""Tests for per-idea pipeline configuration."""

import json
from pathlib import Path

import pytest

from incubator.core.blackboard import Blackboard

DEFAULT_PIPELINE = {
    "stages": ["ideation", "implementation", "validation", "release"],
    "post_ready": ["competitive", "research"],
    "gating": {"default": "auto", "overrides": {}},
    "preset": "full-pipeline",
}


@pytest.fixture
def bb(tmp_path):
    ideas_dir = tmp_path / "ideas"
    template_dir = ideas_dir / "_template"
    template_dir.mkdir(parents=True)
    (template_dir / "status.json").write_text(json.dumps({
        "id": "", "title": "", "phase": "submitted",
        "created_at": "", "updated_at": "",
    }))
    (template_dir / "idea.md").write_text("# Idea\n")
    return Blackboard(ideas_dir)


def test_get_pipeline_returns_default_when_missing(bb):
    """Ideas without pipeline config get the default pipeline."""
    idea_id = bb.create_idea("Test Idea", "Description")
    pipeline = bb.get_pipeline(idea_id)
    assert pipeline["stages"] == ["ideation", "implementation", "validation", "release"]
    assert pipeline["gating"]["default"] == "auto"


def test_get_pipeline_default_is_deep_copy(bb):
    """Modifying returned default doesn't corrupt the module-level DEFAULT_PIPELINE."""
    idea_id = bb.create_idea("Test Idea", "Description")
    pipeline = bb.get_pipeline(idea_id)
    pipeline["stages"].append("custom-agent")
    pipeline["gating"]["overrides"]["release"] = "human-review"
    # Re-fetch — should be clean default again
    pipeline2 = bb.get_pipeline(idea_id)
    assert "custom-agent" not in pipeline2["stages"]
    assert "release" not in pipeline2["gating"]["overrides"]


def test_set_pipeline(bb):
    """Setting pipeline config persists to status.json."""
    idea_id = bb.create_idea("Test Idea", "Description")
    custom = {
        "stages": ["ideation", "validation"],
        "post_ready": [],
        "gating": {"default": "llm-decides", "overrides": {}},
        "preset": "quick-validate",
    }
    bb.set_pipeline(idea_id, custom)
    pipeline = bb.get_pipeline(idea_id)
    assert pipeline["stages"] == ["ideation", "validation"]
    assert pipeline["gating"]["default"] == "llm-decides"


def test_next_stage_returns_first_uncompleted(bb):
    """next_stage() returns the first stage not yet serviced."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    assert bb.next_stage(idea_id) == "ideation"

    # Mark ideation as serviced with "proceed" result
    bb.update_status(idea_id, last_serviced_by={"ideation": "2026-03-11T10:00:00Z"})
    bb.update_status(idea_id, stage_results={"ideation": "proceed"})
    assert bb.next_stage(idea_id) == "implementation"


def test_next_stage_reruns_stage_on_iterate(bb):
    """next_stage() re-runs a stage when its result is 'iterate'."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    bb.update_status(idea_id, last_serviced_by={"ideation": "2026-03-11T10:00:00Z"})
    bb.update_status(idea_id, stage_results={"ideation": "iterate"})
    # Should re-run ideation, not advance to implementation
    assert bb.next_stage(idea_id) == "ideation"


def test_next_stage_iterate_only_affects_that_stage(bb):
    """iterate on ideation doesn't prevent implementation from being next after ideation proceeds."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    bb.update_status(idea_id, last_serviced_by={
        "ideation": "2026-03-11T10:00:00Z",
        "implementation": "2026-03-11T11:00:00Z",
    })
    bb.update_status(idea_id, stage_results={
        "ideation": "proceed",
        "implementation": "iterate",
    })
    # Ideation is done (proceed), implementation needs re-run (iterate)
    assert bb.next_stage(idea_id) == "implementation"


def test_next_stage_returns_none_when_pipeline_complete(bb):
    """next_stage() returns None when all stages are done."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    bb.update_status(idea_id, last_serviced_by={
        "ideation": "2026-03-11T10:00:00Z",
        "implementation": "2026-03-11T11:00:00Z",
        "validation": "2026-03-11T12:00:00Z",
        "release": "2026-03-11T13:00:00Z",
    })
    bb.update_status(idea_id, stage_results={
        "ideation": "proceed",
        "implementation": "proceed",
        "validation": "proceed",
        "release": "proceed",
    })
    assert bb.next_stage(idea_id) is None


def test_is_ready_false_when_stages_remain(bb):
    """is_ready() is False when pipeline has uncompleted stages."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    assert bb.is_ready(idea_id) is False


def test_is_ready_true_when_all_stages_done(bb):
    """is_ready() is True when all stages have been serviced."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    bb.update_status(idea_id, last_serviced_by={
        "ideation": "2026-03-11T10:00:00Z",
        "implementation": "2026-03-11T11:00:00Z",
        "validation": "2026-03-11T12:00:00Z",
        "release": "2026-03-11T13:00:00Z",
    })
    bb.update_status(idea_id, stage_results={
        "ideation": "proceed",
        "implementation": "proceed",
        "validation": "proceed",
        "release": "proceed",
    })
    assert bb.is_ready(idea_id) is True


def test_get_gating_mode_uses_default(bb):
    """get_gating_mode() returns the default when no override exists."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    assert bb.get_gating_mode(idea_id, "ideation") == "auto"


def test_get_gating_mode_uses_override(bb):
    """get_gating_mode() returns the per-agent override when it exists."""
    idea_id = bb.create_idea("Test Idea", "Description")
    custom = json.loads(json.dumps(DEFAULT_PIPELINE))
    custom["gating"] = {"default": "auto", "overrides": {"release": "human-review"}}
    bb.set_pipeline(idea_id, custom)
    assert bb.get_gating_mode(idea_id, "release") == "human-review"
    assert bb.get_gating_mode(idea_id, "ideation") == "auto"


def test_pipeline_has_role(bb):
    """pipeline_has_role() checks stages and post_ready."""
    idea_id = bb.create_idea("Test Idea", "Description")
    bb.set_pipeline(idea_id, DEFAULT_PIPELINE)
    assert bb.pipeline_has_role(idea_id, "ideation") is True
    assert bb.pipeline_has_role(idea_id, "competitive") is True
    assert bb.pipeline_has_role(idea_id, "nonexistent") is False
