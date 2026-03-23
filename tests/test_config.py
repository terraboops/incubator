"""Tests for configuration settings and project root discovery."""

import json
from pathlib import Path

import pytest

from trellis.config import Settings, find_project_root


def test_find_project_root_finds_marker(tmp_path):
    (tmp_path / ".trellis").write_text(json.dumps({"version": "0.2.0"}))
    assert find_project_root(start=tmp_path) == tmp_path


def test_find_project_root_walks_up(tmp_path):
    (tmp_path / ".trellis").write_text(json.dumps({"version": "0.2.0"}))
    deep = tmp_path / "sub" / "deep"
    deep.mkdir(parents=True)
    assert find_project_root(start=deep) == tmp_path


def test_find_project_root_raises_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="trellis init"):
        find_project_root(start=tmp_path)


def test_find_project_root_handles_filesystem_root():
    with pytest.raises(FileNotFoundError):
        find_project_root(start=Path("/"))


def test_pool_defaults():
    """Pool settings have sensible defaults."""
    s = Settings(
        anthropic_api_key="test",
        telegram_bot_token="test",
        telegram_chat_id="test",
    )
    assert s.pool_size == 3
    assert s.job_timeout_minutes == 60
    assert s.producer_interval_seconds == 10


def test_pool_settings_from_env(monkeypatch):
    """Pool settings can be overridden via env vars."""
    monkeypatch.setenv("POOL_SIZE", "5")
    monkeypatch.setenv("JOB_TIMEOUT_MINUTES", "90")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    s = Settings()
    assert s.pool_size == 5
    assert s.job_timeout_minutes == 90
