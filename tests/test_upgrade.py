import json
import shutil
from pathlib import Path
from typer.testing import CliRunner
from trellis.cli import app

runner = CliRunner()


def _scaffold(tmp_path):
    """Scaffold a project for testing."""
    result = runner.invoke(app, ["init", str(tmp_path / "proj")])
    assert result.exit_code == 0
    return tmp_path / "proj"


def test_upgrade_preserves_learnings(tmp_path, monkeypatch):
    proj = _scaffold(tmp_path)
    monkeypatch.chdir(proj)
    # Write learnings
    learnings = proj / "agents" / "ideation" / "knowledge" / "learnings.md"
    learnings.parent.mkdir(parents=True, exist_ok=True)
    learnings.write_text("# My learnings\nImportant stuff")

    result = runner.invoke(app, ["agent", "upgrade", "--all"])
    assert result.exit_code == 0
    assert learnings.read_text() == "# My learnings\nImportant stuff"


def test_upgrade_preserves_claude_sessions(tmp_path, monkeypatch):
    proj = _scaffold(tmp_path)
    monkeypatch.chdir(proj)
    sessions = proj / "agents" / "ideation" / ".claude" / "projects" / "session.jsonl"
    sessions.parent.mkdir(parents=True, exist_ok=True)
    sessions.write_text("session data")

    result = runner.invoke(app, ["agent", "upgrade", "--all"])
    assert sessions.read_text() == "session data"


def test_upgrade_dry_run(tmp_path, monkeypatch):
    proj = _scaffold(tmp_path)
    monkeypatch.chdir(proj)
    # Modify a prompt
    prompt = proj / "agents" / "ideation" / "prompt.py"
    original = prompt.read_text()
    prompt.write_text("modified")

    result = runner.invoke(app, ["agent", "upgrade", "--dry-run"])
    assert result.exit_code == 0
    assert prompt.read_text() == "modified"  # unchanged
