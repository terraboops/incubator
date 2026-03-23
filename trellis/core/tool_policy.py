"""SDK can_use_tool callback for defense-in-depth tool policy.

This is Layer 3 of the security model — runs at the SDK protocol level,
before tool calls reach the OS. Catches violations early with descriptive
error messages so agents can course-correct instead of hitting kernel EPERMs.

Even if this policy is bypassed (SDK bug, future API change), nono's
kernel sandbox (Layer 1) will block the actual filesystem/network access.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

logger = logging.getLogger(__name__)

# Patterns that indicate dangerous Bash commands regardless of role
BASH_BLOCKLIST = [
    "security find-generic-password",
    "security find-internet-password",
    "security dump-keychain",
    "sudo ",
    "su ",
    "osascript",       # AppleScript (can exfiltrate via UI)
    "launchctl",       # launchd manipulation
    "rm -rf /",
    "rm -rf ~",
    "pkill",
    "killall",
    "> /dev/",
    "curl.*ssh",       # SSH key exfiltration via curl
]


def _is_subpath(path: Path, parent: Path) -> bool:
    """Return True if path is equal to or inside parent."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def make_tool_policy(
    role: str,
    allowed_read_dirs: list[Path],
    allowed_write_dirs: list[Path],
):
    """Return a can_use_tool callback implementing the given directory policy.

    Args:
        role: Agent role name (for logging).
        allowed_read_dirs: Paths the agent may read from.
        allowed_write_dirs: Paths the agent may write to.
    """
    async def policy(tool_name: str, tool_input: dict, context) -> PermissionResultAllow | PermissionResultDeny:
        # --- Bash blocklist ---
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            for pattern in BASH_BLOCKLIST:
                if pattern in cmd:
                    logger.warning(
                        "Tool policy blocked Bash for role=%s: pattern=%r cmd=%r",
                        role, pattern, cmd[:200],
                    )
                    return PermissionResultDeny(
                        message=f"Blocked: '{pattern}' is not allowed. If this is needed, add it to sandbox_allowed_commands in the agent config."
                    )

        # --- Read path scoping ---
        if tool_name in ("Read", "Glob", "Grep"):
            path_str = tool_input.get("file_path") or tool_input.get("path", "")
            if path_str and allowed_read_dirs:
                p = Path(path_str)
                if not any(_is_subpath(p, d) for d in allowed_read_dirs):
                    logger.warning(
                        "Tool policy blocked %s for role=%s: path=%s not in allowed dirs",
                        tool_name, role, path_str,
                    )
                    return PermissionResultDeny(
                        message=f"Read denied: '{path_str}' is outside allowed directories."
                    )

        # --- Write path scoping ---
        if tool_name in ("Write", "Edit"):
            path_str = tool_input.get("file_path", "")
            if path_str and allowed_write_dirs:
                p = Path(path_str)
                if not any(_is_subpath(p, d) for d in allowed_write_dirs):
                    logger.warning(
                        "Tool policy blocked %s for role=%s: path=%s not in allowed dirs",
                        tool_name, role, path_str,
                    )
                    return PermissionResultDeny(
                        message=f"Write denied: '{path_str}' is outside allowed directories."
                    )
            elif path_str and not allowed_write_dirs:
                # Watcher/read-only role: no writes at all
                return PermissionResultDeny(
                    message=f"Write denied: this agent role ({role}) is read-only."
                )

        return PermissionResultAllow()

    return policy


def make_role_policy(role: str, idea_id: str, project_root: Path, blackboard_dir: Path):
    """Build a tool policy from standard per-role defaults.

    | Role           | Read dirs                        | Write dirs                   |
    |----------------|----------------------------------|------------------------------|
    | implementation | project_root, blackboard/{id}    | workspace/{id}, blackboard/{id} |
    | ideation       | project_root, blackboard/        | blackboard/{id}              |
    | validation     | project_root, blackboard/        | blackboard/{id}              |
    | release        | project_root, blackboard/        | workspace/{id}, blackboard/{id} |
    | watchers       | blackboard/                      | (none — MCP only)            |
    """
    idea_dir = blackboard_dir / idea_id if idea_id and idea_id != "__all__" else blackboard_dir
    workspace_dir = project_root / "workspace" / idea_id if idea_id and idea_id != "__all__" else None

    if role in ("implementation", "release"):
        read_dirs = [project_root, blackboard_dir]
        write_dirs = [idea_dir]
        if workspace_dir:
            write_dirs.append(workspace_dir)
    elif role in ("ideation", "validation"):
        read_dirs = [project_root, blackboard_dir]
        write_dirs = [idea_dir]
    else:
        # Watchers, prioritizer, global agents: read blackboard only
        read_dirs = [blackboard_dir]
        write_dirs = []

    return make_tool_policy(role, read_dirs, write_dirs)
