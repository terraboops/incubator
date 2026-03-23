import json
from pathlib import Path
from typer.testing import CliRunner
from trellis.cli import app

runner = CliRunner()


def test_init_creates_project(tmp_path):
    target = tmp_path / "myproject"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0
    assert (target / ".trellis").exists()
    assert (target / "agents" / "ideation" / "prompt.py").exists()
    assert (target / "agents" / "artifact-check" / "prompt.py").exists()
    assert (target / "registry.yaml").exists()
    assert (target / "blackboard" / "ideas" / "_template" / "status.json").exists()
    assert (target / "workspace").is_dir()
    assert (target / "pool").is_dir()
    assert (target / "global-system-prompt.md").exists()


def test_init_marker_content(tmp_path):
    target = tmp_path / "proj"
    runner.invoke(app, ["init", str(target)])
    marker = json.loads((target / ".trellis").read_text())
    assert "version" in marker
    assert "created" in marker


def test_init_refuses_existing(tmp_path):
    target = tmp_path / "proj"
    runner.invoke(app, ["init", str(target)])
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1


def test_init_force_overwrites(tmp_path):
    target = tmp_path / "proj"
    runner.invoke(app, ["init", str(target)])
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0


def test_init_current_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".trellis").exists()
