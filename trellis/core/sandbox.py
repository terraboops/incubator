"""Nono sandbox CLI flag building for Trellis agents.

Builds NONO_FLAGS strings consumed by trellis/nono-wrapper.sh at runtime.
All paths are passed as --read/--allow CLI flags, NOT via --config profile
files (nono-ts JSON format is incompatible with nono CLI 0.15.0's parser).

The wrapper script uses --profile claude-code as the base, which provides
~/.claude, keychain, .gitconfig, tmp dirs, etc.

nono-py is an optional dependency used only for profile validation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trellis.core.registry import AgentConfig

logger = logging.getLogger(__name__)


def build_nono_flags(
    config: "AgentConfig",
    idea_id: str,
    project_root: Path,
    blackboard_dir: Path,
    workspace_dir: Path | None = None,
    claude_config_dir: str | None = None,
) -> str:
    """Build the NONO_FLAGS string with all paths and permissions as CLI flags.

    These flags stack with --profile claude-code in the wrapper script.
    Requires nono >= 0.15.0.
    """
    flags: list[str] = []

    # Allow the agent's working directory
    flags.append("--allow-cwd")

    # All agents can read the project root
    flags.append(f"--read {project_root}")

    # Agent's claude config dir needs write access for session files, history, etc.
    # It is typically inside project_root (read-only), so we must explicitly allow it.
    if claude_config_dir:
        flags.append(f"--allow {claude_config_dir}")

    # Blackboard dir: read/write for specific idea, read-only for global
    if idea_id and idea_id != "__all__":
        idea_dir = blackboard_dir / idea_id
        idea_dir.mkdir(parents=True, exist_ok=True)
        flags.append(f"--allow {idea_dir}")
    else:
        flags.append(f"--read {blackboard_dir}")

    # Workspace dir for implementation/release agents
    if workspace_dir:
        if idea_id and idea_id != "__all__":
            ws = workspace_dir / idea_id
            ws.mkdir(parents=True, exist_ok=True)
            flags.append(f"--allow {ws}")
        else:
            flags.append(f"--allow {workspace_dir}")

    # Extra paths from config
    for p in config.sandbox_extra_read_paths:
        if Path(p).exists():
            flags.append(f"--read {p}")
    for p in config.sandbox_extra_write_paths:
        if Path(p).exists():
            flags.append(f"--allow {p}")

    # Allowed commands
    for cmd in config.sandbox_allowed_commands:
        flags.append(f"--allow-command {cmd}")

    # Proxy credentials — injects API keys via nono's reverse proxy.
    # NOTE: This does NOT work for Claude Code OAuth auth (returns 400).
    # Claude Code OAuth relies on env forwarding (CLAUDE_CONFIG_DIR, CLAUDE_*)
    # and keychain read access (provided by the claude-code nono profile).
    # Only use proxy-credential for non-OAuth services (e.g., future API keys).
    for cred in config.sandbox_proxy_credentials:
        flags.append(f"--proxy-credential {cred}")

    # Claude binary auth endpoints — always required in any sandboxed agent.
    # Without these, the nono proxy intercepts and blocks auth traffic, causing
    # immediate 403 "Failed to authenticate" errors before any API call.
    for host in ("api.anthropic.com", "claude.ai"):
        flags.append(f"--proxy-allow {host}")

    # Proxy host allowlist
    for host in config.sandbox_allowed_hosts:
        flags.append(f"--proxy-allow {host}")

    # Port bindings
    for port in config.sandbox_allowed_ports:
        flags.append(f"--allow-port {port}")

    # Credential maps
    for mapping in config.sandbox_credential_maps:
        flags.append(f"--env-credential-map {mapping}")

    return " ".join(flags)
