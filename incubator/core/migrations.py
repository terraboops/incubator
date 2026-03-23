"""Registry migration system.

Versioned migrations that bring registry.yaml files up to spec with
the latest incubator data model. Each migration has a check() and apply()
method, mirroring the autonav pattern.

Migrations are mechanical (pure data transformations) or LLM-assisted
(require human review before applying). LLM-assisted migrations show a
diff and ask for confirmation before writing.

Usage:
    incubator migrate [--registry /path/to/registry.yaml] [--dry-run] [--yes]
"""

from __future__ import annotations

import copy
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import yaml

logger = logging.getLogger(__name__)

# Bump this when adding new migrations
REGISTRY_VERSION = "0.4.0"

# Type for user confirmation callback
ConfirmFn = Callable[[str, str], Awaitable[bool]]


@dataclass
class MigrationCheck:
    needed: bool
    reason: str
    affected_agents: list[str] | None = None


@dataclass
class MigrationResult:
    success: bool
    message: str
    agents_modified: list[str]
    errors: list[str] | None = None


class Migration(ABC):
    """Base class for registry migrations."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Semver version this migration brings the registry to."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this migration does."""

    @property
    def llm_assisted(self) -> bool:
        """If True, requires human review before applying."""
        return False

    @abstractmethod
    def check(self, registry_data: dict) -> MigrationCheck:
        """Check if this migration is needed for the given registry data."""

    @abstractmethod
    def apply(self, registry_data: dict) -> tuple[dict, list[str]]:
        """Apply this migration.

        Returns (updated_data, list_of_modified_agent_names).
        Does NOT write to disk — caller handles that after confirmation.
        """


# ── v0.4.0: Add sandbox fields to all agents ────────────────────────────────

_SANDBOX_DEFAULTS_BY_ROLE = {
    "ideation":     {"sandbox_enabled": False, "sandbox_ssh": False, "sandbox_rollback": False,
                     "sandbox_proxy_credentials": ["anthropic"], "sandbox_allowed_hosts": [],
                     "sandbox_allowed_commands": []},
    "implementation": {"sandbox_enabled": False, "sandbox_ssh": True, "sandbox_rollback": True,
                       "sandbox_proxy_credentials": ["anthropic", "github"],
                       "sandbox_allowed_hosts": ["github.com", "api.github.com"],
                       "sandbox_allowed_commands": []},
    "validation":   {"sandbox_enabled": False, "sandbox_ssh": False, "sandbox_rollback": False,
                     "sandbox_proxy_credentials": ["anthropic"], "sandbox_allowed_hosts": []},
    "release":      {"sandbox_enabled": False, "sandbox_ssh": True, "sandbox_rollback": True,
                     "sandbox_proxy_credentials": ["anthropic", "github"],
                     "sandbox_allowed_hosts": ["github.com", "api.github.com", "pypi.org"],
                     "sandbox_allowed_commands": []},
}
_SANDBOX_COMMON_DEFAULTS = {
    "sandbox_enabled": False,
    "sandbox_ssh": False,
    "sandbox_rollback": False,
    "sandbox_proxy_credentials": ["anthropic"],
    "sandbox_allowed_hosts": [],
    "sandbox_allowed_commands": [],
    "sandbox_allowed_ports": [],
    "sandbox_extra_read_paths": [],
    "sandbox_extra_write_paths": [],
    "sandbox_credential_maps": [],
    "sandbox_profile": "claude-code",
    "sandbox_verify_attestations": False,
}
_SANDBOX_KEYS = set(_SANDBOX_COMMON_DEFAULTS)


class AddSandboxFieldsMigration(Migration):
    version = "0.4.0"
    description = "Add sandbox security fields to all agent entries (defaults: sandbox_enabled=false)"

    def check(self, registry_data: dict) -> MigrationCheck:
        agents = registry_data.get("agents", [])
        missing = [
            a.get("name", "?") for a in agents
            if isinstance(a, dict) and not _SANDBOX_KEYS.issubset(a.keys())
        ]
        if not missing:
            return MigrationCheck(needed=False, reason="All agents already have sandbox fields")
        return MigrationCheck(
            needed=True,
            reason=f"{len(missing)} agent(s) missing sandbox fields",
            affected_agents=missing,
        )

    def apply(self, registry_data: dict) -> tuple[dict, list[str]]:
        data = copy.deepcopy(registry_data)
        modified = []
        for agent in data.get("agents", []):
            if not isinstance(agent, dict):
                continue
            name = agent.get("name", "")
            phase = agent.get("phase", name)
            # Use role-specific defaults if available
            role_defaults = _SANDBOX_DEFAULTS_BY_ROLE.get(phase, {})
            changed = False
            for key, default in _SANDBOX_COMMON_DEFAULTS.items():
                if key not in agent:
                    # Override with role-specific default if present
                    agent[key] = role_defaults.get(key, default)
                    changed = True
            if changed:
                modified.append(name)
        return data, modified


class RemoveIdeationBashAgentMigration(Migration):
    version = "0.4.0-tool-policy"
    description = "Remove Bash and Agent tools from ideation agents (security hardening)"

    def check(self, registry_data: dict) -> MigrationCheck:
        agents = registry_data.get("agents", [])
        affected = []
        for a in agents:
            if not isinstance(a, dict):
                continue
            phase = a.get("phase", a.get("name", ""))
            tools = a.get("tools", [])
            if phase == "ideation" and ("Bash" in tools or "Agent" in tools):
                affected.append(a.get("name", "?"))
        if not affected:
            return MigrationCheck(needed=False, reason="Ideation agents already lack Bash/Agent tools")
        return MigrationCheck(
            needed=True,
            reason=f"Ideation agent(s) still have Bash or Agent tool: {affected}",
            affected_agents=affected,
        )

    def apply(self, registry_data: dict) -> tuple[dict, list[str]]:
        data = copy.deepcopy(registry_data)
        modified = []
        for agent in data.get("agents", []):
            if not isinstance(agent, dict):
                continue
            phase = agent.get("phase", agent.get("name", ""))
            if phase == "ideation":
                tools = agent.get("tools", [])
                new_tools = [t for t in tools if t not in ("Bash", "Agent")]
                if new_tools != tools:
                    agent["tools"] = new_tools
                    modified.append(agent.get("name", "?"))
        return data, modified


# ── Registry of all migrations ─────────────────────────────────────────────

ALL_MIGRATIONS: list[Migration] = [
    AddSandboxFieldsMigration(),
    RemoveIdeationBashAgentMigration(),
]


# ── Runner ────────────────────────────────────────────────────────────────

def load_registry_data(path: Path) -> dict:
    if not path.exists():
        return {"agents": []}
    return yaml.safe_load(path.read_text()) or {}


def save_registry_data(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def check_all(registry_data: dict) -> list[tuple[Migration, MigrationCheck]]:
    """Return list of (migration, check) for all needed migrations."""
    return [
        (m, check)
        for m in ALL_MIGRATIONS
        for check in [m.check(registry_data)]
        if check.needed
    ]


async def apply_migration(
    migration: Migration,
    registry_data: dict,
    confirm: ConfirmFn,
) -> MigrationResult:
    """Apply a single migration with user confirmation if LLM-assisted."""
    try:
        updated, modified = migration.apply(registry_data)

        if not modified:
            return MigrationResult(success=True, message="Nothing to change", agents_modified=[])

        # For LLM-assisted migrations, always require human review
        if migration.llm_assisted:
            import difflib
            before = yaml.dump(registry_data, default_flow_style=False, sort_keys=False)
            after = yaml.dump(updated, default_flow_style=False, sort_keys=False)
            diff = "".join(difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="registry.yaml (before)",
                tofile="registry.yaml (after)",
                n=3,
            ))
            approved = await confirm(
                f"Apply LLM-assisted migration: {migration.description}",
                f"Changes:\n{diff}"
            )
            if not approved:
                return MigrationResult(
                    success=False,
                    message="Migration declined by user",
                    agents_modified=[],
                )

        return MigrationResult(
            success=True,
            message=f"Migration {migration.version} applied: {len(modified)} agent(s) updated",
            agents_modified=modified,
            _updated_data=updated,  # type: ignore[call-arg]
        )
    except Exception as e:
        logger.exception("Migration %s failed", migration.version)
        return MigrationResult(
            success=False,
            message=str(e),
            agents_modified=[],
            errors=[str(e)],
        )


@dataclass
class MigrationResult:
    success: bool
    message: str
    agents_modified: list[str]
    errors: list[str] | None = None
    _updated_data: dict | None = None  # Internal: holds updated data after apply


async def run_migrations(
    registry_path: Path,
    confirm: ConfirmFn,
    dry_run: bool = False,
    auto_yes: bool = False,
) -> list[MigrationResult]:
    """Check and apply all needed migrations to the given registry.yaml.

    Args:
        registry_path: Path to registry.yaml
        confirm: Callback for user confirmation (action, details) -> bool
        dry_run: If True, check but don't apply
        auto_yes: If True, apply mechanical migrations without prompting

    Returns list of MigrationResult for each migration that ran.
    """
    data = load_registry_data(registry_path)
    needed = check_all(data)

    if not needed:
        logger.info("Registry %s is up to date — no migrations needed.", registry_path)
        return []

    results = []
    for migration, check in needed:
        logger.info(
            "Migration needed: %s — %s (affects: %s)",
            migration.version, check.reason, check.affected_agents,
        )

        if dry_run:
            results.append(MigrationResult(
                success=True,
                message=f"[dry-run] Would apply: {migration.description}",
                agents_modified=check.affected_agents or [],
            ))
            continue

        # Auto-confirm mechanical migrations if --yes flag set
        if auto_yes and not migration.llm_assisted:
            async def _auto_confirm(action, details):
                return True
            result_confirm = _auto_confirm
        else:
            result_confirm = confirm

        result = await apply_migration(migration, data, result_confirm)
        results.append(result)

        # Update in-memory data for subsequent migrations
        if result.success and result._updated_data is not None:
            data = result._updated_data
            save_registry_data(registry_path, data)

    return results
