"""Tests for CLI pool integration."""

from unittest.mock import MagicMock, patch, AsyncMock
import pytest


def test_serve_command_has_no_pool_flag():
    """serve command accepts --no-pool flag."""
    from incubator.cli import app
    from typer.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(app, ["serve", "--no-pool", "--help"])
    assert result.exit_code == 0


def test_run_command_exists():
    """run command is registered."""
    from incubator.cli import app
    from typer.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0


def test_pool_enabled_flag():
    """set_pool_enabled controls whether pool starts with the app."""
    from incubator.web.api.app import set_pool_enabled, _pool_enabled_flag
    set_pool_enabled(True)
    assert _pool_enabled_flag() is True
    set_pool_enabled(False)
    assert _pool_enabled_flag() is False
