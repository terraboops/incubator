"""Tests for trellis serve --background/--stop daemon support."""

import json
import signal
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from trellis.cli import app

runner = CliRunner()


def _setup_project(tmp_path):
    """Create minimal trellis project structure and return patched settings."""
    (tmp_path / ".trellis").write_text(json.dumps({"version": "0.2.0"}))
    (tmp_path / "pool").mkdir(exist_ok=True)

    mock_settings = MagicMock()
    mock_settings.project_root = tmp_path
    mock_settings.web_host = "127.0.0.1"
    mock_settings.web_port = 8000
    return mock_settings


def test_serve_background_creates_pidfile(tmp_path):
    settings = _setup_project(tmp_path)

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with patch("trellis.cli.get_settings", return_value=settings), \
         patch("subprocess.Popen", return_value=mock_proc):
        result = runner.invoke(app, ["serve", "--background"])

    assert result.exit_code == 0, result.output
    pid_file = tmp_path / "pool" / "trellis.pid"
    assert pid_file.exists()
    assert pid_file.read_text() == "12345"


def test_serve_stop_sends_sigterm(tmp_path):
    settings = _setup_project(tmp_path)
    (tmp_path / "pool" / "trellis.pid").write_text("99999")

    with patch("trellis.cli.get_settings", return_value=settings), \
         patch("trellis.cli.os.kill") as mock_kill, \
         patch("trellis.cli.time.sleep"):
        # first call succeeds (SIGTERM), second raises OSError (process gone)
        mock_kill.side_effect = [None, OSError]
        result = runner.invoke(app, ["serve", "--stop"])

    assert result.exit_code == 0, result.output
    mock_kill.assert_any_call(99999, signal.SIGTERM)


def test_serve_stop_no_pidfile(tmp_path):
    settings = _setup_project(tmp_path)

    with patch("trellis.cli.get_settings", return_value=settings):
        result = runner.invoke(app, ["serve", "--stop"])

    assert result.exit_code == 1
    assert "No PID file" in result.output


def test_serve_stop_stale_pid(tmp_path):
    settings = _setup_project(tmp_path)
    (tmp_path / "pool" / "trellis.pid").write_text("99999")

    with patch("trellis.cli.get_settings", return_value=settings), \
         patch("trellis.cli.os.kill") as mock_kill:
        # SIGTERM fails because process doesn't exist
        mock_kill.side_effect = OSError
        result = runner.invoke(app, ["serve", "--stop"])

    assert result.exit_code == 0
    assert "not running" in result.output.lower()
    assert not (tmp_path / "pool" / "trellis.pid").exists()
