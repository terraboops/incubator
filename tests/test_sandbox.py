"""Tests for trellis/core/sandbox.py — profile generation and flag building."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trellis.core.registry import AgentConfig
from trellis.core.sandbox import build_nono_flags


# ── build_nono_flags tests (no nono-py required) ──────────────────────────

def test_build_nono_flags_defaults():
    config = AgentConfig(name="test", description="t")
    flags = build_nono_flags(config)
    assert "--proxy-credential anthropic" in flags


def test_build_nono_flags_multiple_proxy_creds():
    config = AgentConfig(
        name="test", description="t",
        sandbox_proxy_credentials=["anthropic", "github"],
    )
    flags = build_nono_flags(config)
    assert "--proxy-credential anthropic" in flags
    assert "--proxy-credential github" in flags


def test_build_nono_flags_allowed_hosts():
    config = AgentConfig(
        name="test", description="t",
        sandbox_allowed_hosts=["api.anthropic.com", "github.com"],
    )
    flags = build_nono_flags(config)
    assert "--allow-proxy api.anthropic.com" in flags
    assert "--allow-proxy github.com" in flags


def test_build_nono_flags_allowed_ports():
    config = AgentConfig(
        name="test", description="t",
        sandbox_allowed_ports=[3000, 8080],
    )
    flags = build_nono_flags(config)
    assert "--allow-port 3000" in flags
    assert "--allow-port 8080" in flags


def test_build_nono_flags_rollback():
    config = AgentConfig(name="test", description="t", sandbox_rollback=True)
    flags = build_nono_flags(config)
    assert "--rollback" in flags


def test_build_nono_flags_no_rollback_by_default():
    config = AgentConfig(name="test", description="t")
    flags = build_nono_flags(config)
    assert "--rollback" not in flags


def test_build_nono_flags_allowed_commands():
    config = AgentConfig(
        name="test", description="t",
        sandbox_allowed_commands=["rm", "docker"],
    )
    flags = build_nono_flags(config)
    assert "--allow-command rm" in flags
    assert "--allow-command docker" in flags


def test_build_nono_flags_credential_maps():
    config = AgentConfig(
        name="test", description="t",
        sandbox_credential_maps=["op://Dev/OpenAI/key -> OPENAI_API_KEY"],
    )
    flags = build_nono_flags(config)
    assert "--env-credential-map" in flags


def test_build_nono_flags_profile():
    config = AgentConfig(name="test", description="t", sandbox_profile="claude-code")
    flags = build_nono_flags(config)
    assert "--profile claude-code" in flags


def test_build_nono_flags_trust_policy(tmp_path):
    trust_policy = tmp_path / "trust-policy.json"
    trust_policy.write_text('{"version": 1}')
    config = AgentConfig(
        name="test", description="t",
        sandbox_verify_attestations=True,
    )
    flags = build_nono_flags(config, project_root=tmp_path)
    assert str(trust_policy) in flags


def test_build_nono_flags_trust_policy_absent(tmp_path):
    config = AgentConfig(
        name="test", description="t",
        sandbox_verify_attestations=True,
    )
    flags = build_nono_flags(config, project_root=tmp_path)
    assert "--trust-policy" not in flags


def test_build_nono_flags_empty_config():
    config = AgentConfig(
        name="test", description="t",
        sandbox_proxy_credentials=[],
        sandbox_allowed_hosts=[],
        sandbox_allowed_ports=[],
        sandbox_allowed_commands=[],
    )
    flags = build_nono_flags(config)
    # Should still have profile
    assert "--profile" in flags


# ── build_profile import test ──────────────────────────────────────────────

def test_build_profile_requires_nono_py(tmp_path):
    """build_profile raises ImportError if nono-py is not installed."""
    config = AgentConfig(name="test", description="t", sandbox_enabled=True)
    blackboard = tmp_path / "blackboard"
    blackboard.mkdir()

    try:
        import nono_py  # noqa: F401
        pytest.skip("nono-py is installed — build_profile test would succeed")
    except ImportError:
        pass

    from trellis.core.sandbox import build_profile
    with pytest.raises(ImportError, match="nono-py"):
        build_profile(config, "test-idea", tmp_path, blackboard)
