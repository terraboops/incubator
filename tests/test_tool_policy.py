"""Tests for trellis/core/tool_policy.py — Bash blocklist and path scoping."""
from __future__ import annotations

from pathlib import Path

import pytest

from trellis.core.tool_policy import make_tool_policy, make_role_policy


# ── Helpers ────────────────────────────────────────────────────────────────

async def _allow(tool, inp, read_dirs=None, write_dirs=None):
    policy = make_tool_policy("test", read_dirs or [], write_dirs or [])
    result = await policy(tool, inp, None)
    return result


# ── Bash blocklist ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "security find-generic-password -s foo -w",
    "security find-internet-password",
    "security dump-keychain",
    "sudo rm -rf /tmp/x",
    "su root",
    "osascript -e 'do shell script'",
    "launchctl load com.evil.plist",
    "rm -rf /",
    "rm -rf ~",
    "pkill Claude",
    "killall Claude",
    "echo foo > /dev/null",
])
async def test_bash_blocklist_denies_dangerous(cmd):
    from claude_agent_sdk import PermissionResultDeny
    result = await _allow("Bash", {"command": cmd})
    assert isinstance(result, PermissionResultDeny)


@pytest.mark.parametrize("cmd", [
    "npm install",
    "git status",
    "git commit -m 'test'",
    "python -m pytest",
    "ls -la /tmp",
    "cat README.md",
    "echo hello",
    "cargo build",
])
async def test_bash_blocklist_allows_normal_commands(cmd):
    from claude_agent_sdk import PermissionResultAllow
    result = await _allow("Bash", {"command": cmd})
    assert isinstance(result, PermissionResultAllow)


# ── Read path scoping ────────────────────────────────────────────────────────

async def test_read_allows_inside_dir(tmp_path):
    from claude_agent_sdk import PermissionResultAllow
    allowed = tmp_path / "project"
    allowed.mkdir()
    result = await _allow("Read", {"file_path": str(allowed / "foo.py")}, read_dirs=[allowed])
    assert isinstance(result, PermissionResultAllow)


async def test_read_denies_outside_dir(tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    allowed = tmp_path / "project"
    allowed.mkdir()
    outside = tmp_path / "secrets"
    result = await _allow("Read", {"file_path": str(outside / "key.pem")}, read_dirs=[allowed])
    assert isinstance(result, PermissionResultDeny)


async def test_read_denies_path_traversal(tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    allowed = tmp_path / "project"
    allowed.mkdir()
    traversal = str(allowed / "../../etc/passwd")
    result = await _allow("Read", {"file_path": traversal}, read_dirs=[allowed])
    assert isinstance(result, PermissionResultDeny)


async def test_read_no_restriction_when_no_dirs():
    """When allowed_read_dirs is empty, reads are not restricted."""
    from claude_agent_sdk import PermissionResultAllow
    result = await _allow("Read", {"file_path": "/etc/passwd"}, read_dirs=[], write_dirs=[])
    assert isinstance(result, PermissionResultAllow)


# ── Write path scoping ────────────────────────────────────────────────────────

async def test_write_allows_inside_dir(tmp_path):
    from claude_agent_sdk import PermissionResultAllow
    allowed = tmp_path / "workspace"
    result = await _allow("Write", {"file_path": str(allowed / "out.txt")}, write_dirs=[allowed])
    assert isinstance(result, PermissionResultAllow)


async def test_write_denies_outside_dir(tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    allowed = tmp_path / "workspace"
    outside = tmp_path / "home" / ".ssh"
    result = await _allow("Write", {"file_path": str(outside / "authorized_keys")}, write_dirs=[allowed])
    assert isinstance(result, PermissionResultDeny)


async def test_write_denied_for_read_only_role(tmp_path):
    """Roles with no write_dirs get all writes denied."""
    from claude_agent_sdk import PermissionResultDeny
    result = await _allow("Write", {"file_path": str(tmp_path / "anything.txt")}, write_dirs=[])
    assert isinstance(result, PermissionResultDeny)


async def test_edit_denied_for_read_only_role(tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    result = await _allow("Edit", {"file_path": str(tmp_path / "file.py")}, write_dirs=[])
    assert isinstance(result, PermissionResultDeny)


# ── make_role_policy ──────────────────────────────────────────────────────────

async def test_role_policy_implementation_allows_workspace(tmp_path):
    from claude_agent_sdk import PermissionResultAllow
    bb = tmp_path / "blackboard"
    workspace = tmp_path / "workspace" / "my-idea"
    workspace.mkdir(parents=True)

    policy = make_role_policy("implementation", "my-idea", tmp_path, bb)
    result = await policy("Write", {"file_path": str(workspace / "app.py")}, None)
    assert isinstance(result, PermissionResultAllow)


async def test_role_policy_watcher_denies_write(tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    bb = tmp_path / "blackboard"

    policy = make_role_policy("competitive-watcher", "my-idea", tmp_path, bb)
    result = await policy("Write", {"file_path": str(bb / "my-idea" / "foo.md")}, None)
    assert isinstance(result, PermissionResultDeny)
