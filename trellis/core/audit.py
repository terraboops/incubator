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

    hooks_dict: dict = {"PostToolUse": [HookMatcher(hooks=[log_tool])]}

    # Merge webhook hooks from boopifier.json if present
    webhook_hooks = _build_webhook_hooks(agent_role, project_root)
    if webhook_hooks:
        for event, matchers in webhook_hooks.items():
            hooks_dict.setdefault(event, []).extend(matchers)

    return hooks_dict


def _build_webhook_hooks(agent_role: str, project_root: Path) -> dict | None:
    """Build SDK hooks that fire webhook calls from agent's boopifier.json.

    The CLI's hook system (settings.json) doesn't apply to SDK-spawned agents,
    so we read the agent's boopifier.json and call webhooks directly via HTTP.
    """
    import urllib.request

    # Find boopifier.json in the agent's claude_home
    boopifier_path = project_root / "agents" / agent_role / ".claude" / "boopifier.json"
    if not boopifier_path.exists():
        return None

    try:
        config = json.loads(boopifier_path.read_text())
    except Exception:
        return None

    webhook_handlers = [h for h in config.get("handlers", []) if h.get("type") == "webhook"]
    if not webhook_handlers:
        return None

    def _make_hook(url: str, event_name: str):
        def _blocking_post(payload: bytes):
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=2)
            except Exception:
                pass

        async def fire_webhook(hook_input, tool_use_id, context):
            import asyncio

            tool_name = hook_input.get("tool_name", "")
            session_id = hook_input.get("session_id", "")
            payload = json.dumps({"text": f"{event_name}|{session_id}|{tool_name}"}).encode()
            # Run blocking HTTP call off the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _blocking_post, payload)
            return {}

        return fire_webhook

    hooks_dict: dict = {}
    for handler in webhook_handlers:
        url = handler.get("config", {}).get("url", "")
        if not url:
            continue
        for event in ("PreToolUse", "PostToolUse", "Stop", "Notification"):
            hook_fn = _make_hook(url, event)
            hooks_dict.setdefault(event, []).append(HookMatcher(hooks=[hook_fn]))

    return hooks_dict
