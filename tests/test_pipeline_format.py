"""Tests for the format-aware pipeline loading and saving."""

from pathlib import Path

import yaml

from trellis.core.pipeline_format import (
    detect_format,
    find_template,
    load_pipeline,
    save_pipeline,
)


SAMPLE_PIPELINE = {
    "name": "test",
    "description": "A test pipeline",
    "agents": ["ideation", "implementation"],
    "post_ready": ["watcher"],
    "parallel_groups": [["ideation", "implementation"], ["watcher"]],
    "gating": {"default": "auto", "overrides": {}},
}

SAMPLE_PROSE = """\
pipeline test:
  description: "A test pipeline"

  parallel:
    session: watcher

  session: ideation
  gate: auto

  session: implementation
  gate: auto
"""


def test_detect_format_prose():
    assert detect_format(Path("foo.prose")) == "prose"


def test_detect_format_yaml():
    assert detect_format(Path("foo.yaml")) == "yaml"
    assert detect_format(Path("foo.yml")) == "yaml"


def test_load_pipeline_yaml(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text(yaml.dump(SAMPLE_PIPELINE, default_flow_style=False))
    result = load_pipeline(p)
    assert result["name"] == "test"
    assert result["agents"] == ["ideation", "implementation"]


def test_load_pipeline_prose(tmp_path):
    p = tmp_path / "test.prose"
    p.write_text(SAMPLE_PROSE)
    result = load_pipeline(p)
    assert result["name"] == "test"
    assert result["agents"] == ["ideation", "implementation"]
    assert result["post_ready"] == ["watcher"]


def test_load_prose_and_yaml_produce_same_structure(tmp_path):
    """Both formats produce the same canonical structure."""
    yaml_path = tmp_path / "test.yaml"
    yaml_path.write_text(yaml.dump(SAMPLE_PIPELINE, default_flow_style=False))
    prose_path = tmp_path / "test.prose"
    prose_path.write_text(SAMPLE_PROSE)

    from_yaml = load_pipeline(yaml_path)
    from_prose = load_pipeline(prose_path)

    assert from_yaml["name"] == from_prose["name"]
    assert from_yaml["agents"] == from_prose["agents"]
    assert from_yaml["post_ready"] == from_prose["post_ready"]
    assert from_yaml["description"] == from_prose["description"]


def test_save_pipeline_yaml(tmp_path):
    p = tmp_path / "out.yaml"
    save_pipeline(p, SAMPLE_PIPELINE, fmt="yaml")
    reloaded = yaml.safe_load(p.read_text())
    assert reloaded["name"] == "test"
    assert reloaded["agents"] == ["ideation", "implementation"]


def test_save_pipeline_prose(tmp_path):
    p = tmp_path / "out.prose"
    save_pipeline(p, SAMPLE_PIPELINE, fmt="prose")
    content = p.read_text()
    assert "pipeline test:" in content
    assert "session: ideation" in content


def test_save_prose_roundtrip(tmp_path):
    """Save as prose then load produces same data."""
    p = tmp_path / "out.prose"
    save_pipeline(p, SAMPLE_PIPELINE, fmt="prose")
    reloaded = load_pipeline(p)
    assert reloaded["name"] == "test"
    assert reloaded["agents"] == ["ideation", "implementation"]
    assert reloaded["post_ready"] == ["watcher"]


def test_find_template_prefers_prose(tmp_path):
    (tmp_path / "test.prose").write_text(SAMPLE_PROSE)
    (tmp_path / "test.yaml").write_text(yaml.dump(SAMPLE_PIPELINE))
    found = find_template(tmp_path, "test")
    assert found is not None
    assert found.suffix == ".prose"


def test_find_template_falls_back_to_yaml(tmp_path):
    (tmp_path / "test.yaml").write_text(yaml.dump(SAMPLE_PIPELINE))
    found = find_template(tmp_path, "test")
    assert found is not None
    assert found.suffix == ".yaml"


def test_find_template_falls_back_to_yml(tmp_path):
    (tmp_path / "test.yml").write_text(yaml.dump(SAMPLE_PIPELINE))
    found = find_template(tmp_path, "test")
    assert found is not None
    assert found.suffix == ".yml"


def test_find_template_returns_none(tmp_path):
    assert find_template(tmp_path, "nonexistent") is None


def test_mixed_directory_loads_all(tmp_path):
    """Both .prose and .yaml files in the same directory are loadable."""
    (tmp_path / "alpha.prose").write_text(SAMPLE_PROSE)
    yaml_data = dict(SAMPLE_PIPELINE, name="beta")
    (tmp_path / "beta.yaml").write_text(yaml.dump(yaml_data, default_flow_style=False))

    from_prose = load_pipeline(tmp_path / "alpha.prose")
    from_yaml = load_pipeline(tmp_path / "beta.yaml")

    assert from_prose["name"] == "test"
    assert from_yaml["name"] == "beta"
