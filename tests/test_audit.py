"""Tests for incubator/core/audit.py — PostToolUse audit hook."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from incubator.core.audit import make_audit_hooks, _configure_audit_handler


async def test_hook_returns_empty_dict(tmp_path):
    hooks = make_audit_hooks("ideation", "test-idea", tmp_path)
    hook_list = hooks["PostToolUse"]
    assert len(hook_list) == 1

    hook_fn = hook_list[0].hooks[0]
    result = await hook_fn(
        {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x.txt"}, "tool_response": ""},
        "tool-use-id-1",
        None,
    )
    assert result == {}


async def test_bash_not_logged(tmp_path, caplog):
    # Reset handler config for test isolation
    import incubator.core.audit as audit_mod
    audit_mod._audit_handler_configured = False
    # Remove existing handlers
    logger = logging.getLogger("incubator.audit")
    logger.handlers = []

    hooks = make_audit_hooks("implementation", "test-idea", tmp_path)
    hook_fn = hooks["PostToolUse"][0].hooks[0]

    with caplog.at_level(logging.INFO, logger="incubator.audit"):
        await hook_fn(
            {"tool_name": "Bash", "tool_input": {"command": "npm install"}, "tool_response": ""},
            "id-2",
            None,
        )

    # Bash should not produce log entries from our hook
    bash_entries = [r for r in caplog.records if r.name == "incubator.audit"]
    assert len(bash_entries) == 0


async def test_read_logged_with_path(tmp_path):
    import incubator.core.audit as audit_mod
    audit_mod._audit_handler_configured = False
    logger = logging.getLogger("incubator.audit")
    logger.handlers = []

    hooks = make_audit_hooks("ideation", "my-idea", tmp_path)
    hook_fn = hooks["PostToolUse"][0].hooks[0]

    audit_file = tmp_path / "pool" / "audit.jsonl"
    await hook_fn(
        {"tool_name": "Read", "tool_input": {"file_path": "/tmp/foo.md"}, "tool_response": ""},
        "id-3",
        None,
    )

    assert audit_file.exists()
    lines = [l for l in audit_file.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["tool"] == "Read"
    assert entry["path"] == "/tmp/foo.md"
    assert entry["agent"] == "ideation"
    assert entry["idea"] == "my-idea"


async def test_web_search_logged_with_query(tmp_path):
    import incubator.core.audit as audit_mod
    audit_mod._audit_handler_configured = False
    logger = logging.getLogger("incubator.audit")
    logger.handlers = []

    hooks = make_audit_hooks("ideation", "my-idea", tmp_path)
    hook_fn = hooks["PostToolUse"][0].hooks[0]

    await hook_fn(
        {"tool_name": "WebSearch", "tool_input": {"query": "market size AI agents"}, "tool_response": ""},
        "id-4",
        None,
    )

    audit_file = tmp_path / "pool" / "audit.jsonl"
    lines = [l for l in audit_file.read_text().splitlines() if l.strip()]
    entry = json.loads(lines[-1])
    assert entry["tool"] == "WebSearch"
    assert "market size" in entry["query"]
