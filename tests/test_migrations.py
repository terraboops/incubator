"""Tests for the registry migration system."""
from __future__ import annotations

from pathlib import Path

import pytest

from incubator.core.migrations import (
    AddSandboxFieldsMigration,
    RemoveIdeationBashAgentMigration,
    check_all,
    load_registry_data,
    save_registry_data,
    run_migrations,
)


def _registry_with_agents(*agents):
    return {"agents": list(agents)}


def _basic_agent(name="test", phase="ideation", tools=None):
    return {"name": name, "phase": phase, "tools": tools or ["Read", "WebSearch"]}


# ── AddSandboxFieldsMigration ──────────────────────────────────────────────

def test_sandbox_migration_needed_when_fields_missing():
    data = _registry_with_agents(_basic_agent())
    m = AddSandboxFieldsMigration()
    check = m.check(data)
    assert check.needed
    assert "test" in check.affected_agents


def test_sandbox_migration_not_needed_when_fields_present():
    agent = _basic_agent()
    # Add all sandbox fields
    agent.update({
        "sandbox_enabled": False,
        "sandbox_ssh": False,
        "sandbox_rollback": False,
        "sandbox_profile": "claude-code",
        "sandbox_proxy_credentials": ["anthropic"],
        "sandbox_allowed_hosts": [],
        "sandbox_allowed_ports": [],
        "sandbox_allowed_commands": [],
        "sandbox_extra_read_paths": [],
        "sandbox_extra_write_paths": [],
        "sandbox_credential_maps": [],
        "sandbox_verify_attestations": False,
    })
    data = _registry_with_agents(agent)
    m = AddSandboxFieldsMigration()
    check = m.check(data)
    assert not check.needed


def test_sandbox_migration_apply_adds_fields():
    data = _registry_with_agents(_basic_agent("ideation", "ideation"))
    m = AddSandboxFieldsMigration()
    updated, modified = m.apply(data)
    assert "ideation" in modified
    agent = updated["agents"][0]
    assert "sandbox_enabled" in agent
    assert agent["sandbox_enabled"] is False


def test_sandbox_migration_apply_role_specific_defaults():
    """Implementation agents get ssh=True by default."""
    data = _registry_with_agents(_basic_agent("implementation", "implementation"))
    m = AddSandboxFieldsMigration()
    updated, modified = m.apply(data)
    agent = updated["agents"][0]
    assert agent["sandbox_ssh"] is True
    assert agent["sandbox_rollback"] is True
    assert "github" in agent["sandbox_proxy_credentials"]


def test_sandbox_migration_does_not_overwrite_existing():
    """If sandbox_ssh is already set to True, don't overwrite."""
    agent = _basic_agent()
    agent["sandbox_ssh"] = True
    # Only partial fields present — but sandbox_ssh should not be overwritten
    data = _registry_with_agents(agent)
    m = AddSandboxFieldsMigration()
    updated, _ = m.apply(data)
    assert updated["agents"][0]["sandbox_ssh"] is True


# ── RemoveIdeationBashAgentMigration ────────────────────────────────────────

def test_remove_bash_agent_needed():
    agent = _basic_agent("ideation", "ideation", tools=["Read", "Bash", "Agent", "WebSearch"])
    data = _registry_with_agents(agent)
    m = RemoveIdeationBashAgentMigration()
    check = m.check(data)
    assert check.needed
    assert "ideation" in check.affected_agents


def test_remove_bash_agent_not_needed():
    agent = _basic_agent("ideation", "ideation", tools=["Read", "WebSearch", "WebFetch"])
    data = _registry_with_agents(agent)
    m = RemoveIdeationBashAgentMigration()
    check = m.check(data)
    assert not check.needed


def test_remove_bash_agent_apply():
    agent = _basic_agent("ideation", "ideation", tools=["Read", "Bash", "Agent", "WebSearch"])
    data = _registry_with_agents(agent)
    m = RemoveIdeationBashAgentMigration()
    updated, modified = m.apply(data)
    tools = updated["agents"][0]["tools"]
    assert "Bash" not in tools
    assert "Agent" not in tools
    assert "Read" in tools
    assert "WebSearch" in tools


def test_remove_bash_agent_only_affects_ideation():
    """Implementation agents keep their Bash tool."""
    agent = _basic_agent("implementation", "implementation", tools=["Read", "Bash", "Glob"])
    data = _registry_with_agents(agent)
    m = RemoveIdeationBashAgentMigration()
    check = m.check(data)
    assert not check.needed


# ── check_all ────────────────────────────────────────────────────────────────

def test_check_all_empty_when_up_to_date():
    agent = _basic_agent("ideation", "ideation")
    # Add all fields
    for k, v in {
        "sandbox_enabled": False, "sandbox_ssh": False, "sandbox_rollback": False,
        "sandbox_profile": "claude-code", "sandbox_proxy_credentials": ["anthropic"],
        "sandbox_allowed_hosts": [], "sandbox_allowed_ports": [],
        "sandbox_allowed_commands": [], "sandbox_extra_read_paths": [],
        "sandbox_extra_write_paths": [], "sandbox_credential_maps": [],
        "sandbox_verify_attestations": False,
    }.items():
        agent[k] = v
    data = _registry_with_agents(agent)
    needed = check_all(data)
    assert needed == []


def test_check_all_returns_multiple():
    agent = _basic_agent("ideation", "ideation", tools=["Read", "Bash"])
    data = _registry_with_agents(agent)
    needed = check_all(data)
    versions = [m.version for m, _ in needed]
    assert "0.4.0" in versions
    assert "0.4.0-tool-policy" in versions


# ── round-trip: disk read → migrate → disk write ────────────────────────────

async def test_run_migrations_round_trip(tmp_path):
    reg = tmp_path / "registry.yaml"
    data = {"agents": [_basic_agent("ideation", "ideation", tools=["Read", "Bash", "WebSearch"])]}
    save_registry_data(reg, data)

    async def confirm(action, details):
        return True

    results = await run_migrations(reg, confirm)
    assert all(r.success for r in results)

    reloaded = load_registry_data(reg)
    agent = reloaded["agents"][0]
    assert "sandbox_enabled" in agent
    assert "Bash" not in agent.get("tools", [])


async def test_run_migrations_dry_run_does_not_write(tmp_path):
    reg = tmp_path / "registry.yaml"
    data = {"agents": [_basic_agent()]}
    save_registry_data(reg, data)

    original = reg.read_text()

    async def confirm(action, details):
        return True

    results = await run_migrations(reg, confirm, dry_run=True)
    assert reg.read_text() == original  # not modified
