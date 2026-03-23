"""PostToolUse audit logging for SDK-level tool visibility.

Layer 4 of the security model. nono's built-in audit handles Bash
commands (with timing, exit codes, network events, filesystem mutations).
This hook handles SDK-level tools (Read, Write, Edit, Glob, Grep, etc.)
that run through the Claude CLI's own code — invisible to nono.

Audit entries are appended to pool/audit.jsonl as newline-delimited JSON.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import HookMatcher

logger = logging.getLogger("trellis.audit")

_audit_handler_configured = False


def _configure_audit_handler(project_root: Path) -> None:
    """Set up FileHandler appending to pool/audit.jsonl (idempotent)."""
    global _audit_handler_configured
    if _audit_handler_configured:
        return

    audit_log = project_root / "pool" / "audit.jsonl"
    audit_log.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(audit_log, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _audit_handler_configured = True


def make_audit_hooks(
    agent_role: str,
    idea_id: str,
    project_root: Path,
) -> dict:
    """Return a hooks dict for ClaudeAgentOptions with a PostToolUse audit logger.

    Bash commands are NOT logged here — nono's native audit covers those
    with richer data (timing, exit code, network events, filesystem mutations).
    """
    _configure_audit_handler(project_root)

    async def log_tool(hook_input, tool_use_id, context):
        tool_name = hook_input.get("tool_name", "unknown")

        # Skip Bash — nono audit handles it with more detail
        if tool_name == "Bash":
            return {}

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": agent_role,
            "idea": idea_id,
            "tool": tool_name,
        }

        # Include path info for file tools
        tool_input = hook_input.get("tool_input", {})
        if tool_name in ("Read", "Write", "Edit"):
            path = tool_input.get("file_path", "")
            if path:
                entry["path"] = path
        elif tool_name in ("Glob", "Grep"):
            path = tool_input.get("path", "")
            pattern = tool_input.get("pattern", "")
            if path:
                entry["path"] = path
            if pattern:
                entry["pattern"] = pattern[:200]
        elif tool_name in ("WebSearch", "WebFetch"):
            entry["query"] = str(tool_input.get("query", tool_input.get("url", "")))[:300]

        logger.info(json.dumps(entry))
        return {}

    return {"PostToolUse": [HookMatcher(hooks=[log_tool])]}
