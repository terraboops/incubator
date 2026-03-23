"""Nono sandbox profile generation and CLI flag building.

Uses nono-py to programmatically build per-role CapabilitySets,
serialize to JSON profiles, and produce NONO_FLAGS strings consumed
by agents/nono-wrapper.sh at runtime.

nono-py is an optional dependency; import errors are raised lazily
(only when sandbox_enabled=True) so the module is always importable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trellis.core.registry import AgentConfig

logger = logging.getLogger(__name__)


def _require_nono():
    """Lazy-import nono_py, raising a clear error if not installed."""
    try:
        import nono_py  # noqa: F401
        return nono_py
    except ImportError as e:
        raise ImportError(
            "nono-py is required for sandbox support. "
            "Install it with: pip install nono-py\n"
            "Also ensure the nono CLI is installed: brew install always-further/tap/nono"
        ) from e


def build_profile(
    config: "AgentConfig",
    idea_id: str,
    project_root: Path,
    blackboard_dir: Path,
) -> Path:
    """Build a nono sandbox profile for the given agent config, write to disk, return path."""
    nono_py = _require_nono()
    CapabilitySet = nono_py.CapabilitySet
    AccessMode = nono_py.AccessMode
    SandboxState = nono_py.SandboxState

    caps = CapabilitySet()

    # All agents can read the project root
    caps.allow_path(str(project_root), AccessMode.READ)

    # All agents can read/write their idea's blackboard dir
    if idea_id and idea_id != "__all__":
        caps.allow_path(str(blackboard_dir / idea_id), AccessMode.READ_WRITE)
    else:
        caps.allow_path(str(blackboard_dir), AccessMode.READ)

    # Temp dirs for build tools
    caps.allow_path("/tmp", AccessMode.READ_WRITE)

    # Extra paths from config
    for p in config.sandbox_extra_read_paths:
        caps.allow_path(p, AccessMode.READ)
    for p in config.sandbox_extra_write_paths:
        caps.allow_path(p, AccessMode.READ_WRITE)

    # Block network by default; nono proxy handles allowlist
    caps.block_network()

    # Serialize profile
    profile_dir = project_root / "pool" / "sandbox-profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    safe_idea = idea_id.replace("/", "_") if idea_id else "global"
    profile_path = profile_dir / f"{config.name}-{safe_idea}.json"

    state = SandboxState.from_caps(caps)
    profile_path.write_text(state.to_json())
    logger.debug("Wrote sandbox profile to %s", profile_path)
    return profile_path


def validate_profile(profile_path: Path, expected_paths: list[str]) -> bool:
    """Verify that expected_paths are covered by the serialized profile."""
    nono_py = _require_nono()
    QueryContext = nono_py.QueryContext

    try:
        data = json.loads(profile_path.read_text())
        ctx = QueryContext.from_dict(data)
        for path in expected_paths:
            if not ctx.is_path_allowed(path):
                logger.warning("Profile %s does not allow path %s", profile_path, path)
                return False
        return True
    except Exception as e:
        logger.warning("Profile validation failed: %s", e)
        return False


def build_nono_flags(config: "AgentConfig", project_root: Path | None = None) -> str:
    """Build the NONO_FLAGS string from AgentConfig fields."""
    flags: list[str] = []

    for cred in config.sandbox_proxy_credentials:
        flags.append(f"--proxy-credential {cred}")

    for host in config.sandbox_allowed_hosts:
        flags.append(f"--allow-proxy {host}")

    for port in config.sandbox_allowed_ports:
        flags.append(f"--allow-port {port}")

    for cmd in config.sandbox_allowed_commands:
        flags.append(f"--allow-command {cmd}")

    if config.sandbox_rollback:
        flags.append("--rollback")

    for mapping in config.sandbox_credential_maps:
        # Each entry is "URI -> ENV_VAR" format
        flags.append(f"--env-credential-map {mapping}")

    if config.sandbox_profile:
        flags.append(f"--profile {config.sandbox_profile}")

    if config.sandbox_verify_attestations and project_root:
        trust_policy = project_root / "trust-policy.json"
        if trust_policy.exists():
            flags.append(f"--trust-policy {trust_policy}")

    return " ".join(flags)
