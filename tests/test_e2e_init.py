import json
from pathlib import Path
from typer.testing import CliRunner
from trellis.cli import app

runner = CliRunner()


def test_full_lifecycle(tmp_path, monkeypatch):
    """End-to-end: init -> verify structure -> config resolves."""
    result = runner.invoke(app, ["init", str(tmp_path / "testproject")])
    assert result.exit_code == 0

    project = tmp_path / "testproject"
    assert (project / ".trellis").exists()
    assert (project / "registry.yaml").exists()
    assert (project / "agents" / "ideation" / "prompt.py").exists()
    assert (project / "agents" / "artifact-check" / "prompt.py").exists()
    assert (project / "blackboard" / "ideas" / "_template" / "status.json").exists()
    assert (project / "workspace").is_dir()
    assert (project / "pool").is_dir()
    assert (project / "global-system-prompt.md").exists()

    # Verify config discovery works
    monkeypatch.chdir(project)
    from trellis.config import find_project_root
    assert find_project_root() == project

    # Verify agent upgrade works
    result = runner.invoke(app, ["agent", "upgrade", "--dry-run"])
    assert result.exit_code == 0
