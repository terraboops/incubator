"""Tests for the Prose pipeline parser and emitter."""

import pytest

from trellis.core.prose_parser import emit_pipeline_prose, parse_pipeline_prose


BASIC_PROSE = """\
pipeline default:
  description: "Standard 4-stage pipeline with watchers"

  parallel:
    session: competitive-watcher
    session: research-watcher

  session: ideation
  gate: auto

  session: implementation
  gate: auto

  session: validation
  gate: auto

  session: release
  gate: auto
"""


def test_parse_basic_pipeline():
    result = parse_pipeline_prose(BASIC_PROSE)
    assert result["name"] == "default"
    assert result["description"] == "Standard 4-stage pipeline with watchers"
    assert result["agents"] == ["ideation", "implementation", "validation", "release"]
    assert result["post_ready"] == ["competitive-watcher", "research-watcher"]
    assert ["competitive-watcher", "research-watcher"] in result["parallel_groups"]
    assert ["ideation", "implementation", "validation", "release"] in result["parallel_groups"]


def test_parse_parallel_groups():
    result = parse_pipeline_prose(BASIC_PROSE)
    # Main agents group + parallel block group
    assert len(result["parallel_groups"]) == 2
    assert result["parallel_groups"][0] == ["ideation", "implementation", "validation", "release"]
    assert result["parallel_groups"][1] == ["competitive-watcher", "research-watcher"]


def test_parse_gate_overrides():
    prose = """\
pipeline reviewed:
  description: "Human-reviewed pipeline"

  session: ideation
  gate: auto

  session: implementation
  gate: human-review

  session: release
  gate: auto
"""
    result = parse_pipeline_prose(prose)
    assert result["gating"]["overrides"]["ideation"] == "auto"
    assert result["gating"]["overrides"]["implementation"] == "human-review"
    assert result["gating"]["overrides"]["release"] == "auto"


def test_parse_with_comments():
    prose = """\
# This is a comment
pipeline simple:
  description: "A simple pipeline"
  # Another comment
  session: ideation
  gate: auto
"""
    result = parse_pipeline_prose(prose)
    assert result["name"] == "simple"
    assert result["agents"] == ["ideation"]


def test_roundtrip():
    """Parse → emit → re-parse produces identical dict."""
    original = parse_pipeline_prose(BASIC_PROSE)
    emitted = emit_pipeline_prose(original)
    reparsed = parse_pipeline_prose(emitted)
    assert reparsed["name"] == original["name"]
    assert reparsed["description"] == original["description"]
    assert reparsed["agents"] == original["agents"]
    assert reparsed["post_ready"] == original["post_ready"]


def test_parse_no_parallel_block():
    prose = """\
pipeline fast:
  description: "Fast prototype"

  session: ideation
  gate: auto

  session: implementation
  gate: auto
"""
    result = parse_pipeline_prose(prose)
    assert result["agents"] == ["ideation", "implementation"]
    assert result["post_ready"] == []
    assert result["parallel_groups"] == [["ideation", "implementation"]]


def test_parse_single_agent():
    prose = """\
pipeline solo:
  description: "Single agent"
  session: ideation
  gate: auto
"""
    result = parse_pipeline_prose(prose)
    assert result["agents"] == ["ideation"]
    assert result["parallel_groups"] == [["ideation"]]


def test_parse_multiple_gate_modes():
    prose = """\
pipeline mixed:
  description: "Mixed gating"

  session: ideation
  gate: auto

  session: validation
  gate: human-review

  session: release
  gate: llm-decides
"""
    result = parse_pipeline_prose(prose)
    assert result["gating"]["overrides"]["ideation"] == "auto"
    assert result["gating"]["overrides"]["validation"] == "human-review"
    assert result["gating"]["overrides"]["release"] == "llm-decides"


def test_emit_basic():
    data = {
        "name": "test",
        "description": "A test pipeline",
        "agents": ["ideation", "release"],
        "post_ready": ["watcher"],
        "parallel_groups": [["ideation", "release"], ["watcher"]],
        "gating": {"default": "auto", "overrides": {}},
    }
    result = emit_pipeline_prose(data)
    assert "pipeline test:" in result
    assert 'description: "A test pipeline"' in result
    assert "session: ideation" in result
    assert "session: release" in result
    assert "session: watcher" in result
    assert "parallel:" in result


def test_emit_with_overrides():
    data = {
        "name": "test",
        "description": "",
        "agents": ["ideation", "release"],
        "post_ready": [],
        "parallel_groups": [["ideation", "release"]],
        "gating": {"default": "auto", "overrides": {"release": "human-review"}},
    }
    result = emit_pipeline_prose(data)
    assert "gate: auto" in result
    assert "gate: human-review" in result


def test_parse_error_missing_pipeline_name():
    with pytest.raises(Exception):
        parse_pipeline_prose("session: ideation\n")


def test_parse_error_empty_input():
    with pytest.raises(Exception):
        parse_pipeline_prose("")
