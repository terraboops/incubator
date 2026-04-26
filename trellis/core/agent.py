"""Base agent wrapping the Claude Agent SDK."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from trellis.comms.notifications import NotificationDispatcher
from trellis.core.activity import ActivityTracker
from trellis.core.audit import make_audit_hooks
from trellis.core.blackboard import Blackboard
from trellis.core.registry import AgentConfig
from trellis.core.tool_policy import make_role_policy
from trellis.tools.blackboard_tools import create_blackboard_mcp_server, create_watcher_mcp_server
from trellis.tools.evolution_tools import create_evolution_mcp_server
from trellis.tools.telegram_tools import create_telegram_mcp_server

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keychain credential mirroring
# ---------------------------------------------------------------------------
# The Claude CLI stores OAuth tokens in macOS Keychain. The service name
# varies by version:
#   - Current: "Claude Code-credentials" (no hash suffix)
#   - Legacy:  "Claude Code-credentials-<hash>" where hash = sha256(config_dir)[:8]
# When we point an agent at its own .claude/ dir, we need the keychain to
# have a matching entry. This function copies the credential from the parent
# process's keychain entry to one the agent's config dir can find.
# ---------------------------------------------------------------------------


def _fire_agent_webhook(agent_role: str, project_root: Path, event: str) -> None:
    """Fire boopifier webhook for an agent event (best-effort, non-blocking)."""
    import json as _json
    import urllib.request

    boopifier_path = project_root / "agents" / agent_role / ".claude" / "boopifier.json"
    if not boopifier_path.exists():
        return
    try:
        config = _json.loads(boopifier_path.read_text())
        for handler in config.get("handlers", []):
            if handler.get("type") != "webhook":
                continue
            url = handler.get("config", {}).get("url", "")
            if not url:
                continue
            payload = _json.dumps({"text": f"{event}|{agent_role}|"}).encode()
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def _keychain_service_hashed(config_dir: str) -> str:
    """Legacy hashed service name: Claude Code-credentials-<hash>."""
    h = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{h}"


KEYCHAIN_SERVICE_PLAIN = "Claude Code-credentials"


def _read_keychain_credential(service: str) -> str | None:
    """Read a credential from macOS Keychain. Returns None on failure."""
    read = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    if read.returncode == 0:
        return read.stdout.strip()
    return None


def _credential_is_fresh(credential: str | None) -> bool:
    """Return True if credential JSON contains a non-expired OAuth token."""
    if not credential:
        return False
    try:
        import time

        d = json.loads(credential)
        exp = d.get("claudeAiOauth", {}).get("expiresAt", 0)
        return bool(exp) and exp > time.time() * 1000
    except Exception:
        return False


def _ensure_agent_auth(agent_config: Path, project_root: Path) -> None:
    """Mirror the parent process's keychain credential for the agent's config dir.

    Tries both the plain service name ("Claude Code-credentials") and the
    legacy hashed name. Also syncs oauthAccount metadata from the parent's
    .claude.json to the agent's .claude.json.
    """
    parent_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    agent_dir = str(agent_config.resolve())

    if parent_dir == agent_dir:
        return  # same dir, nothing to do

    # Try to read parent credential: hashed parent entry first (most specific/fresh),
    # then fall back to plain (which may be stale from a different session).
    # Skip any entry whose OAuth token is already expired.
    hashed_parent_svc = _keychain_service_hashed(parent_dir)
    credential = _read_keychain_credential(hashed_parent_svc)
    if not _credential_is_fresh(credential):
        credential = _read_keychain_credential(KEYCHAIN_SERVICE_PLAIN)
    if not _credential_is_fresh(credential):
        logger.warning(
            "Could not find a fresh parent keychain credential (tried '%s' and '%s')",
            hashed_parent_svc,
            KEYCHAIN_SERVICE_PLAIN,
        )
        return

    # Write credential for the agent's config dir (both plain and hashed)
    agent_hashed_svc = _keychain_service_hashed(agent_dir)
    account = os.environ.get("USER", "unknown")
    for svc in (KEYCHAIN_SERVICE_PLAIN, agent_hashed_svc):
        write = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                svc,
                "-a",
                account,
                "-w",
                credential,
                "-U",  # update if exists
            ],
            capture_output=True,
            text=True,
        )
        if write.returncode != 0:
            logger.debug("Keychain write to '%s' failed: %s", svc, write.stderr.strip())

    # Sync oauthAccount metadata from parent's .claude.json to agent's .claude.json
    parent_json = Path(parent_dir) / ".claude.json"
    if not parent_json.exists():
        # Also check ~/.claude/.claude.json (nested)
        parent_json_nested = Path(parent_dir) / ".claude" / ".claude.json"
        if parent_json_nested.exists():
            parent_json = parent_json_nested

    agent_json = agent_config / ".claude.json"
    if parent_json.exists():
        try:
            parent_data = json.loads(parent_json.read_text())
            oauth = parent_data.get("oauthAccount")
            if oauth:
                agent_data = json.loads(agent_json.read_text()) if agent_json.exists() else {}
                if agent_data.get("oauthAccount") != oauth:
                    agent_data["oauthAccount"] = oauth
                    agent_json.parent.mkdir(parents=True, exist_ok=True)
                    agent_json.write_text(json.dumps(agent_data, indent=2))
                    logger.info("Synced oauthAccount to %s", agent_json)
        except Exception as e:
            logger.warning("Failed to sync oauthAccount: %s", e)


LLM_DECIDES_CONTEXT = """
<self-assessment>
Before completing your work, critically evaluate what you've produced:

1. Are there aspects of your analysis or output you're uncertain about?
2. Did you make assumptions that should be validated by a human?
3. Is the quality of your work sufficient for the next pipeline stage?
4. Are there risks or edge cases you couldn't fully evaluate?

If you have ANY meaningful uncertainty about your output quality:
- Set phase_recommendation to "needs_review"
- Explain your specific concerns in phase_reasoning
- Err on the side of flagging — false positives are far better than missed issues

If you are confident your work is solid:
- Set phase_recommendation to "proceed" as normal
</self-assessment>
"""


@dataclass
class AgentResult:
    success: bool
    output: str = ""
    cost_usd: float = 0.0
    error: str | None = None
    transcript: list[dict] = field(default_factory=list)
    sandbox_failure: bool = False
    session_id: str | None = None
    stop_reason: str | None = None
    sandbox_suggestions: list[dict] = field(default_factory=list)


class BaseAgent(ABC):
    """Abstract base for all Trellis agents."""

    def __init__(
        self,
        config: AgentConfig,
        blackboard: Blackboard,
        dispatcher: NotificationDispatcher,
        project_root: Path,
    ) -> None:
        self.config = config
        self.blackboard = blackboard
        self.dispatcher = dispatcher
        self.project_root = project_root

    @abstractmethod
    def get_system_prompt(self, idea_id: str) -> str:
        """Return the system prompt for this agent, contextualized to the idea."""

    def get_knowledge_dir(self) -> Path:
        """Return the knowledge directory for this agent type."""
        return self.project_root / "agents" / (self.config.phase or self.config.name) / "knowledge"

    def get_subagents(self) -> dict[str, AgentDefinition]:
        """Override to define subagents for this agent."""
        return {}

    def get_working_dir(self, idea_id: str) -> str:
        """Return agent dir so .claude/ sessions land in the project.

        Creates the directory if it doesn't exist, falling back to project root.
        """
        agent_dir = self.project_root / "agents" / self.config.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return str(agent_dir)

    def _build_deadline_context(self, deadline: datetime) -> str:
        """Build time-awareness context for the system prompt."""
        now = datetime.now(timezone.utc)
        remaining = (deadline - now).total_seconds()
        minutes = max(0, int(remaining / 60))
        return (
            f"\n\n<time-budget>\n"
            f"You have about {minutes} minutes for this task.\n"
            f"Current time: {now.strftime('%H:%M UTC')}\n"
            f"Window ends: {deadline.strftime('%H:%M UTC')}\n"
            f"Do your best work within this time. If you can't finish everything,\n"
            f"save your progress and note what's left for next time.\n"
            f"</time-budget>"
        )

    def _message_to_dict(self, message) -> dict | None:
        """Convert an SDK message to a serializable dict for the transcript."""
        ts = datetime.now(timezone.utc).isoformat()

        if isinstance(message, AssistantMessage):
            blocks = []
            for block in message.content or []:
                if isinstance(block, TextBlock):
                    blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input
                            if isinstance(block.input, (dict, str))
                            else str(block.input),
                        }
                    )
                elif hasattr(block, "type"):
                    # Thinking blocks, etc.
                    d = {"type": block.type}
                    if hasattr(block, "thinking"):
                        d["thinking"] = block.thinking
                    blocks.append(d)
                else:
                    blocks.append({"type": "unknown", "repr": repr(block)[:500]})
            return {"role": "assistant", "blocks": blocks, "timestamp": ts}

        if isinstance(message, SystemMessage):
            return {
                "role": "system",
                "subtype": getattr(message, "subtype", None),
                "timestamp": ts,
            }

        if isinstance(message, ResultMessage):
            return {
                "role": "result",
                "result": message.result,
                "stop_reason": message.stop_reason,
                "cost_usd": message.total_cost_usd,
                "session_id": getattr(message, "session_id", None),
                "usage": message.usage
                if isinstance(getattr(message, "usage", None), dict)
                else None,
                "timestamp": ts,
            }

        # Catch-all for other message types
        return {
            "role": "unknown",
            "type": type(message).__name__,
            "repr": repr(message)[:1000],
            "timestamp": ts,
        }

    def _live_log_path(self, idea_id: str) -> Path:
        """Path for the partial in-progress transcript (deleted on completion)."""
        log_dir = self.blackboard.idea_dir(idea_id) / "agent-logs"
        log_dir.mkdir(exist_ok=True)
        return log_dir / f".live-{self.config.name}.json"

    _SANDBOX_FAILURE_PATTERNS = (
        "operation not permitted",
        "eperm",
        "nono:",
        "permission denied",
        "sandbox",
    )

    @staticmethod
    def parse_sandbox_suggestions(stderr_lines: list[str]) -> list[dict]:
        """Parse nono stderr lines into actionable sandbox config suggestions."""
        import re

        suggestions: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for line in stderr_lines:
            low = line.lower()
            # nono: denied read /some/path
            m = re.search(r"nono:\s*denied\s+(read|write)\s+(/\S+)", low)
            if m:
                action, path = m.group(1), m.group(2)
                field = (
                    "sandbox_extra_read_paths" if action == "read" else "sandbox_extra_write_paths"
                )
                key = (field, path)
                if key not in seen:
                    seen.add(key)
                    suggestions.append({"type": field, "value": path, "stderr_line": line.strip()})
                continue

            # nono: denied connect some.host:443
            m = re.search(r"nono:\s*denied\s+connect\s+(\S+)", low)
            if m:
                host = m.group(1).split(":")[0]
                key = ("sandbox_allowed_hosts", host)
                if key not in seen:
                    seen.add(key)
                    suggestions.append(
                        {
                            "type": "sandbox_allowed_hosts",
                            "value": host,
                            "stderr_line": line.strip(),
                        }
                    )
                continue

            # operation not permitted: /some/path
            m = re.search(r"operation not permitted[:\s]+(/\S+)", low)
            if m:
                path = m.group(1)
                key = ("sandbox_extra_write_paths", path)
                if key not in seen:
                    seen.add(key)
                    suggestions.append(
                        {
                            "type": "sandbox_extra_write_paths",
                            "value": path,
                            "stderr_line": line.strip(),
                        }
                    )

    def _write_live_log(
        self,
        idea_id: str,
        transcript: list[dict],
        process_stderr: list[str] | None = None,
        sandbox_failure: bool = False,
    ) -> None:
        """Overwrite the live log with the current partial transcript."""
        try:
            path = self._live_log_path(idea_id)
            data = {
                "agent": self.config.name,
                "idea_id": idea_id,
                "model": self.config.model,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "entries": transcript,
                "process_stderr": process_stderr or [],
                "sandbox_failure": sandbox_failure,
            }
            path.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.warning(
                "Live log write failed for agent '%s' on '%s': %s",
                self.config.name,
                idea_id,
                e,
            )

    def _clear_live_log(self, idea_id: str) -> None:
        """Delete the live log file when the agent finishes."""
        try:
            self._live_log_path(idea_id).unlink(missing_ok=True)
        except Exception:
            pass

    def _save_transcript(
        self,
        idea_id: str,
        transcript: list[dict],
        prompt: str,
        system_prompt: str,
        *,
        sandbox_failure: bool = False,
        process_stderr: list[str] | None = None,
    ) -> None:
        """Save the full agent transcript to the blackboard.

        Skips saving when the transcript has no real messages (e.g. auth
        failures that produce only a system init + immediate error).
        """
        real_messages = [e for e in transcript if e.get("role") not in ("system", None)]
        if not real_messages:
            logger.info(
                "Skipping empty transcript for agent '%s' on '%s'",
                self.config.name,
                idea_id,
            )
            return

        # Extract session_id from the result entry
        session_id = None
        for entry in reversed(transcript):
            if entry.get("role") == "result" and entry.get("session_id"):
                session_id = entry["session_id"]
                break

        log_dir = self.blackboard.idea_dir(idea_id) / "agent-logs"
        log_dir.mkdir(exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        log_file = log_dir / f"{self.config.name}-{ts}.json"

        log_data = {
            "agent": self.config.name,
            "idea_id": idea_id,
            "model": self.config.model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "sandbox_failure": sandbox_failure,
            "process_stderr": process_stderr or [],
            "system_prompt": system_prompt,
            "user_prompt": prompt,
            "max_turns": self.config.max_turns,
            "max_budget_usd": self.config.max_budget_usd,
            "tools": self.config.tools,
            "transcript": transcript,
        }
        log_file.write_text(json.dumps(log_data, indent=2, default=str))
        logger.info("Saved transcript to %s", log_file)

        # Update projection index so agent logs are immediately queryable
        if self.blackboard.projection:
            try:
                self.blackboard.projection.index_agent_log(
                    self.config.name, idea_id, log_file.name, log_data
                )
            except Exception:
                pass

    async def run(
        self, idea_id: str, max_turns_override: int | None = None, deadline: datetime | None = None
    ) -> AgentResult:
        """Run this agent against an idea."""
        logger.info("Running agent '%s' on idea '%s'", self.config.name, idea_id)

        # Global agents skip activity tracking (no specific idea to track)
        if idea_id == "__all__":
            return await self._run_inner(
                idea_id, max_turns_override=max_turns_override, deadline=deadline
            )

        # Register with activity tracker
        tracker = ActivityTracker(self.blackboard.base_dir.parent / ".activity.json")
        try:
            idea_title = ""
            try:
                status = self.blackboard.get_status(idea_id)
                idea_title = status.get("title", idea_id)
            except Exception:
                idea_title = idea_id
            tracker.start(self.config.name, idea_id, idea_title, self.config.model)
        except Exception:
            logger.warning("Failed to register activity start", exc_info=True)

        try:
            return await self._run_inner(
                idea_id, max_turns_override=max_turns_override, deadline=deadline
            )
        finally:
            try:
                tracker.stop(self.config.name, idea_id)
            except Exception:
                logger.warning("Failed to register activity stop", exc_info=True)

    def _get_idea_knowledge_dir(self, idea_id: str) -> Path:
        """Return the per-idea knowledge directory for this agent."""
        return self.blackboard.idea_dir(idea_id) / "agent-knowledge" / self.config.name

    def _is_refinement_run(self, idea_id: str) -> bool:
        """Check if this idea has been released before (meaning we're in refinement mode)."""
        status = self.blackboard.get_status(idea_id)
        history = status.get("phase_history", [])
        return any(entry.get("to") == "released" for entry in history)

    def _load_idea_knowledge(self, idea_id: str) -> str:
        """Load accumulated per-idea knowledge for this agent."""
        knowledge_dir = self._get_idea_knowledge_dir(idea_id)
        if not knowledge_dir.exists():
            return ""
        parts = []
        for f in sorted(knowledge_dir.iterdir()):
            if f.is_file() and f.suffix in (".md", ".txt", ".json", ".yaml"):
                parts.append(f"### {f.name}\n{f.read_text()}")
        return "\n\n".join(parts)

    def _build_prior_work_manifest(self, idea_id: str) -> str:
        """Build a manifest of existing blackboard files for the Memento Loop.

        This ensures every agent starts with full awareness of prior work,
        preventing redundant effort and enabling building on previous phases.
        """
        idea_dir = self.blackboard.idea_dir(idea_id)
        if not idea_dir.exists():
            return ""

        files = sorted(
            f for f in idea_dir.iterdir() if f.is_file() and f.name not in ("status.json",)
        )
        if not files:
            return ""

        lines = ["\n\n## Prior Work on the Blackboard"]
        lines.append(
            "The following files already exist for this idea. "
            "Read the ones relevant to your role before planning your work.\n"
        )

        for f in files:
            size_kb = f.stat().st_size / 1024
            suffix = f.suffix.lstrip(".")
            lines.append(f"- `{f.name}` ({suffix}, {size_kb:.1f} KB)")

        lines.append("")
        lines.append(
            "Use `read_blackboard` to read any file above. "
            "Build on prior work — don't duplicate it."
        )
        return "\n".join(lines)

    def _build_feedback_context(self, idea_id: str) -> str:
        """Build context for unacknowledged feedback assigned to this agent."""
        pending = self.blackboard.get_pending_feedback(idea_id, self.config.name)
        if not pending:
            return ""

        lines = [
            "\n\n## Human Feedback Requiring Your Attention",
            f"You have {len(pending)} unacknowledged feedback item(s). For each one:",
            "1. Read the referenced artifact if you haven't already",
            "2. Decide if the feedback is relevant to your expertise",
            "3. If relevant, update the artifact to address the feedback",
            "4. Call `acknowledge_feedback` with the feedback ID and a brief note on what you did",
            "5. If the feedback is outside your expertise, still acknowledge it with a note like "
            '"Not in my area of expertise — this is better addressed by [role]"',
            "",
        ]
        for entry in pending:
            lines.append(f"### Feedback `{entry['id']}` on `{entry.get('artifact', 'general')}`")
            if entry.get("selected_text"):
                lines.append(f"> {entry['selected_text']}")
            lines.append(f"Comment: {entry['comment']}")
            lines.append(f"Submitted: {entry.get('created_at', 'unknown')}")
            lines.append("")

        return "\n".join(lines)

    def _get_refinement_context(self, idea_id: str) -> str:
        """Build refinement-mode context for the prompt."""
        status = self.blackboard.get_status(idea_id)
        history = status.get("phase_history", [])
        release_count = sum(1 for entry in history if entry.get("to") == "released")
        iteration_count = status.get("iteration_count", 0)

        return (
            f"\n\n## REFINEMENT MODE (cycle #{release_count})\n"
            f"This idea has been through {iteration_count} iterations and released "
            f"{release_count} time(s). You are now in REFINEMENT mode.\n\n"
            f"**Your job is NOT to redo your work from scratch.** Instead:\n"
            f"1. Read ALL existing artifacts on the blackboard using `list_files` and `read_blackboard`\n"
            f"2. Critically analyze your previous outputs — find weaknesses, blind spots, "
            f"unstated assumptions\n"
            f"3. Do NEW research to validate or disprove claims in existing artifacts\n"
            f"4. Scrutinize numbers, projections, and estimates against real-world data\n"
            f"5. IMPROVE existing artifacts by overwriting them with better versions\n"
            f"6. Write a brief refinement note to your knowledge file using `write_knowledge`\n\n"
            f"Think like a devil's advocate. Challenge everything. Make it bulletproof.\n"
        )

    def _setup_sandbox_env(self, env: dict, idea_id: str) -> Path | None:
        """Configure nono sandbox env vars and return cli_path if sandbox is enabled.

        Mutates env in-place. Returns the nono-wrapper.sh path if sandbox is
        enabled, or None to use the SDK default cli path.

        All paths are passed as CLI flags via NONO_FLAGS (not --config profile
        files, which are incompatible between nono-ts and nono CLI 0.15.0).
        """
        if not self.config.sandbox_enabled:
            return None

        # Check if nono is actually installed
        import shutil

        if not shutil.which("nono"):
            logger.warning(
                "Sandbox enabled for agent '%s' but nono is not installed. "
                "Running without sandbox. Install nono to enable sandboxing.",
                self.config.name,
            )
            return None

        try:
            from trellis.core.sandbox import build_nono_flags

            nono_flags = build_nono_flags(
                config=self.config,
                idea_id=idea_id,
                project_root=self.project_root,
                blackboard_dir=self.blackboard.base_dir,
                workspace_dir=self.project_root / "workspace",
                claude_config_dir=env.get("CLAUDE_CONFIG_DIR"),
            )
            env["NONO_FLAGS"] = nono_flags

            if self.config.sandbox_ssh:
                ssh_sock = os.environ.get("SSH_AUTH_SOCK", "")
                if ssh_sock:
                    env["SSH_AUTH_SOCK"] = ssh_sock

            cli_path = Path(__file__).resolve().parent.parent / "nono-wrapper.sh"
            logger.info(
                "Sandbox enabled for agent '%s' on '%s', flags: %s",
                self.config.name,
                idea_id,
                nono_flags,
            )
            return cli_path
        except Exception as e:
            logger.error(
                "Failed to build nono sandbox flags for agent '%s': %s. "
                "Falling back to unsandboxed mode.",
                self.config.name,
                e,
            )
            return None

    async def _setup_forge_identity(self, env: dict) -> None:
        """Resolve OIDC identity and obtain a forge token, injecting into env.

        This is a no-op if forge federation is not configured.  Failures are
        logged but never prevent the agent from running — forge access is
        best-effort.  Mutates *env* in-place.
        """
        try:
            from trellis.config import get_settings
            from trellis.core.identity import (
                create_token_manager,
                resolve_forge_exchanger,
                resolve_identity_provider,
            )

            settings = get_settings()
            if not settings.forge_type:
                return

            provider = resolve_identity_provider(
                provider_type=settings.identity_provider,
                spiffe_endpoint_socket=settings.spiffe_endpoint_socket,
                spiffe_trust_domain=settings.spiffe_trust_domain,
            )
            exchanger = resolve_forge_exchanger(
                forge_type=settings.forge_type,
                forge_url=settings.forge_url,
                github_app_id=settings.github_app_id,
                github_app_installation_id=settings.github_app_installation_id,
                github_app_private_key_path=settings.github_app_private_key_path,
                gitlab_token_exchange_url=settings.gitlab_token_exchange_url,
            )
            manager = await create_token_manager(
                provider=provider,
                exchanger=exchanger,
                audience=settings.forge_token_audience,
            )
            if manager is None:
                return

            cred = await manager.get_forge_token(
                repos=self.config.forge_repos or None,
                permissions=self.config.forge_permissions or None,
            )
            if cred is None:
                logger.warning(
                    "Forge token exchange returned None for agent '%s'",
                    self.config.name,
                )
                return

            env["FORGE_TOKEN"] = cred.token
            env["FORGE_TYPE"] = settings.forge_type
            env["FORGE_URL"] = settings.forge_url
            logger.info(
                "Forge token injected for agent '%s': type=%s, fingerprint=%s",
                self.config.name,
                settings.forge_type,
                cred.fingerprint,
            )
        except Exception as e:
            logger.warning(
                "Forge identity setup failed for agent '%s': %s. "
                "Agent will run without forge access.",
                self.config.name,
                e,
            )

    async def _run_global(
        self, max_turns_override: int | None = None, deadline: datetime | None = None
    ) -> AgentResult:
        """Run in global mode (phase='*') — iterate over all ideas, no idea-specific context."""
        bb_server = create_blackboard_mcp_server(self.blackboard, "__all__")
        tg_server = create_telegram_mcp_server(self.dispatcher, "__all__")
        ev_server = create_evolution_mcp_server(self.get_knowledge_dir())
        mcp_servers = {"blackboard": bb_server, "telegram": tg_server, "evolution": ev_server}

        system_prompt = self.config.system_prompt_override or self.get_system_prompt("__all__")
        if deadline:
            system_prompt += self._build_deadline_context(deadline)

        # List all ideas for the agent to iterate over
        all_ideas = self.blackboard.list_ideas()
        prompt = (
            f"## Active Ideas\n"
            f"The following ideas are in trellis: {', '.join(sorted(all_ideas))}\n\n"
            f"Use the blackboard tools to read and inspect artifacts for each idea. "
            f"When done, summarize your findings."
        )

        env = {"CLAUDECODE": ""}
        if self.config.env:
            env.update(self.config.env)

        await self._setup_forge_identity(env)
        cli_path = self._setup_sandbox_env(env, "__all__")

        options = ClaudeAgentOptions(
            cwd=self.get_working_dir("__all__"),
            system_prompt=system_prompt,
            allowed_tools=self.config.tools,
            max_turns=max_turns_override or self.config.max_turns,
            model=self.config.model,
            mcp_servers=mcp_servers,
            permission_mode=self.config.permission_mode
            if not self.config.sandbox_enabled
            else None,
            env=env,
            cli_path=cli_path,
            can_use_tool=make_role_policy(
                self.config.name,
                "__all__",
                self.project_root,
                self.blackboard.base_dir,
            ),
            hooks=make_audit_hooks(self.config.name, "__all__", self.project_root),
        )
        if self.config.max_budget_usd > 0:
            options.max_budget_usd = self.config.max_budget_usd
        if self.config.thinking:
            options.thinking = self.config.thinking
        if self.config.setting_sources:
            options.setting_sources = self.config.setting_sources

        subagents = self.get_subagents()
        if subagents:
            options.agents = subagents

        output_parts = []
        transcript = []
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    entry = self._message_to_dict(message)
                    if entry:
                        transcript.append(entry)
                    if isinstance(message, ResultMessage):
                        output_parts.append(message.result or "")
                        cost = message.total_cost_usd or 0.0
                        logger.info(
                            "Agent '%s' completed global run (stop: %s, cost: $%.2f)",
                            self.config.name,
                            message.stop_reason,
                            cost,
                        )
                        return AgentResult(
                            success=True,
                            output="\n".join(output_parts),
                            cost_usd=cost,
                            transcript=transcript,
                        )
        except Exception as e:
            logger.exception("Agent '%s' failed in global mode", self.config.name)
            return AgentResult(success=False, error=str(e), transcript=transcript)

        return AgentResult(success=True, output="\n".join(output_parts), transcript=transcript)

    async def _run_inner(
        self, idea_id: str, max_turns_override: int | None = None, deadline: datetime | None = None
    ) -> AgentResult:
        """Inner run logic, wrapped by activity tracking."""

        # Global agent mode: no idea-specific context
        if idea_id == "__all__":
            return await self._run_global(max_turns_override=max_turns_override, deadline=deadline)

        # Ensure per-idea knowledge directory exists
        idea_knowledge_dir = self._get_idea_knowledge_dir(idea_id)
        idea_knowledge_dir.mkdir(parents=True, exist_ok=True)

        # Build MCP servers for custom tools
        # Cadence agents (watchers) get a restricted MCP with read-only + register_feedback
        if self.config.cadence:
            bb_server = create_watcher_mcp_server(
                self.blackboard, idea_id, agent_role=self.config.name
            )
        else:
            bb_server = create_blackboard_mcp_server(
                self.blackboard, idea_id, agent_role=self.config.name
            )
        tg_server = create_telegram_mcp_server(self.dispatcher, idea_id)
        ev_server = create_evolution_mcp_server(self.get_knowledge_dir())

        mcp_servers = {
            "blackboard": bb_server,
            "telegram": tg_server,
            "evolution": ev_server,
        }

        # Build the system prompt: global prefix + values + agent-specific prompt
        global_prompt = ""
        global_prompt_path = self.project_root / "global-system-prompt.md"
        if global_prompt_path.exists():
            global_prompt = global_prompt_path.read_text().strip() + "\n\n"

        values = ""
        values_path = self.project_root / "values.md"
        if values_path.exists():
            values = "## Values\n" + values_path.read_text().strip() + "\n\n"

        agent_prompt = self.config.system_prompt_override or self.get_system_prompt(idea_id)
        system_prompt = global_prompt + values + agent_prompt

        # Inject deadline awareness if running within a pool window
        if deadline:
            system_prompt += self._build_deadline_context(deadline)

        # Inject LLM-decides gating context when configured
        try:
            gating_mode = self.blackboard.get_gating_mode(idea_id, self.config.name)
            if gating_mode == "llm-decides":
                system_prompt += LLM_DECIDES_CONTEXT
        except AttributeError:
            pass  # get_gating_mode not yet available (pre-migration)

        # Load accumulated knowledge (global agent knowledge + per-idea knowledge)
        knowledge_context = ""
        from trellis.tools.knowledge_io import load_objects, format_for_prompt

        knowledge_objects = load_objects(self.get_knowledge_dir())
        if knowledge_objects:
            knowledge_context += (
                f"\n\n## Global Agent Learnings\n{format_for_prompt(knowledge_objects)}"
            )
        idea_knowledge = self._load_idea_knowledge(idea_id)
        if idea_knowledge:
            knowledge_context += f"\n\n## Your Previous Notes on This Idea\n{idea_knowledge}"

        # Check if this is a refinement run
        is_refining = self._is_refinement_run(idea_id)
        refinement_context = self._get_refinement_context(idea_id) if is_refining else ""

        # Check for pending human feedback
        feedback_context = self._build_feedback_context(idea_id)

        # Read the idea description
        idea_content = self.blackboard.read_file(idea_id, "idea.md")

        # --- Memento Loop: build prior-work manifest ---
        prior_work = self._build_prior_work_manifest(idea_id)

        prompt = (
            f"## Idea\n{idea_content}\n"
            f"{prior_work}"
            f"{knowledge_context}"
            f"{refinement_context}"
            f"{feedback_context}\n\n"
            f"Now execute your role for idea '{idea_id}'. "
            f"Read any prior artifacts listed above before planning your work. "
            f"Use the blackboard tools to write your outputs. "
        )

        if feedback_context:
            prompt += (
                "You have pending human feedback — address it first by reading the "
                "referenced artifacts, taking action if relevant to your role, and "
                "calling `acknowledge_feedback` for each item. "
            )

        prompt += (
            "When done, use `declare_artifacts` to register what you created, "
            "then `set_phase_recommendation` to indicate what should happen next."
        )

        subagents = self.get_subagents()

        # Merge env: always unset CLAUDECODE to allow nested sessions,
        # then layer on any per-agent env vars from config
        env = {"CLAUDECODE": ""}
        if self.config.env:
            env.update(self.config.env)

        # Point CLAUDE_CONFIG_DIR at the agent's own .claude folder (which may
        # contain hooks, CLAUDE.md, settings, etc.).  The SDK stores OAuth
        # tokens in macOS Keychain under "Claude Code-credentials-<hash>"
        # where <hash> = sha256(CLAUDE_CONFIG_DIR)[:8].  We must ensure the
        # agent's config dir has a matching keychain entry.
        if self.config.claude_home:
            agent_config = Path(self.config.claude_home)
            if not agent_config.is_absolute():
                agent_config = self.project_root / agent_config
            agent_config_str = str(agent_config.resolve())
            env["CLAUDE_CONFIG_DIR"] = agent_config_str

            # Also copy .claude.json oauthAccount metadata so the SDK knows
            # which account to look up in the keychain.
            _ensure_agent_auth(agent_config, self.project_root)

        await self._setup_forge_identity(env)
        cli_path = self._setup_sandbox_env(env, idea_id)
        if cli_path:
            logger.info("Using nono wrapper at: %s (exists: %s)", cli_path, cli_path.exists())

        options = ClaudeAgentOptions(
            cwd=self.get_working_dir(idea_id),
            system_prompt=system_prompt,
            allowed_tools=self.config.tools,
            max_turns=max_turns_override or self.config.max_turns,
            model=self.config.model,
            mcp_servers=mcp_servers,
            permission_mode=self.config.permission_mode
            if not self.config.sandbox_enabled
            else None,
            env=env,
            cli_path=cli_path,
            can_use_tool=make_role_policy(
                self.config.name,
                idea_id,
                self.project_root,
                self.blackboard.base_dir,
            ),
            hooks=make_audit_hooks(self.config.name, idea_id, self.project_root),
        )
        # Only set budget if non-zero (0 = unlimited)
        if self.config.max_budget_usd > 0:
            options.max_budget_usd = self.config.max_budget_usd
        if self.config.thinking:
            options.thinking = self.config.thinking
        if self.config.setting_sources:
            options.setting_sources = self.config.setting_sources
        if subagents:
            options.agents = subagents

        output_parts = []
        transcript = []
        process_stderr: list[str] = []
        sandbox_failure = False

        def _on_stderr(line: str) -> None:
            nonlocal sandbox_failure
            process_stderr.append(line)
            logger.warning("Agent '%s' stderr on '%s': %s", self.config.name, idea_id, line)
            low = line.lower()
            if any(pat in low for pat in self._SANDBOX_FAILURE_PATTERNS):
                sandbox_failure = True
            self._write_live_log(idea_id, transcript, process_stderr, sandbox_failure)

        options.stderr = _on_stderr

        # Fire webhook for session start (SDK hooks don't cover SessionStart)
        _fire_agent_webhook(self.config.name, self.project_root, "SessionStart")

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    entry = self._message_to_dict(message)
                    if entry:
                        transcript.append(entry)
                        self._write_live_log(idea_id, transcript, process_stderr, sandbox_failure)

                    if isinstance(message, ResultMessage):
                        output_parts.append(message.result or "")
                        cost = message.total_cost_usd or 0.0
                        logger.info(
                            "Agent '%s' completed on '%s' (stop: %s, cost: $%.2f)",
                            self.config.name,
                            idea_id,
                            message.stop_reason,
                            cost,
                        )
                        self._save_transcript(
                            idea_id,
                            transcript,
                            prompt,
                            system_prompt,
                            sandbox_failure=sandbox_failure,
                            process_stderr=process_stderr,
                        )
                        self._clear_live_log(idea_id)
                        suggestions = self.parse_sandbox_suggestions(process_stderr)
                        _fire_agent_webhook(self.config.name, self.project_root, "SessionEnd")
                        return AgentResult(
                            success=True,
                            output="\n".join(output_parts),
                            cost_usd=cost,
                            transcript=transcript,
                            sandbox_failure=sandbox_failure,
                            session_id=getattr(message, "session_id", None),
                            stop_reason=message.stop_reason,
                            sandbox_suggestions=suggestions,
                        )
        except Exception as e:
            _fire_agent_webhook(self.config.name, self.project_root, "SessionEnd")
            logger.exception("Agent '%s' failed on '%s'", self.config.name, idea_id)
            self._save_transcript(
                idea_id,
                transcript,
                prompt,
                system_prompt,
                sandbox_failure=sandbox_failure,
                process_stderr=process_stderr,
            )
            self._clear_live_log(idea_id)
            return AgentResult(
                success=False,
                error=str(e),
                transcript=transcript,
                sandbox_failure=sandbox_failure,
            )

        # Stream ended without a ResultMessage — SDK closed the connection
        # before completion. This is anomalous (rate limit, transport error,
        # OAuth refresh, etc.). Surface it as a failure rather than silently
        # claiming success with $0 cost; the worker uses stop_reason=None as
        # the rate-limit signal and the pool will back off / retry.
        logger.warning(
            "Agent '%s' on '%s' stream ended without ResultMessage "
            "(transcript_entries=%d, output_parts=%d)",
            self.config.name,
            idea_id,
            len(transcript),
            len(output_parts),
        )
        _fire_agent_webhook(self.config.name, self.project_root, "SessionEnd")
        self._save_transcript(
            idea_id,
            transcript,
            prompt,
            system_prompt,
            sandbox_failure=sandbox_failure,
            process_stderr=process_stderr,
        )
        self._clear_live_log(idea_id)
        return AgentResult(
            success=False,
            error="agent stream ended without ResultMessage",
            output="\n".join(output_parts),
            transcript=transcript,
            sandbox_failure=sandbox_failure,
        )
