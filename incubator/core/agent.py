"""Base agent wrapping the Claude Agent SDK."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

from incubator.comms.notifications import NotificationDispatcher
from incubator.core.activity import ActivityTracker
from incubator.core.blackboard import Blackboard
from incubator.core.registry import AgentConfig
from incubator.tools.blackboard_tools import create_blackboard_mcp_server
from incubator.tools.evolution_tools import create_evolution_mcp_server
from incubator.tools.telegram_tools import create_telegram_mcp_server

logger = logging.getLogger(__name__)

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


class BaseAgent(ABC):
    """Abstract base for all incubator agents."""

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
        return self.project_root / "incubator" / "agents" / (self.config.phase or self.config.name) / "knowledge"

    def get_subagents(self) -> dict[str, AgentDefinition]:
        """Override to define subagents for this agent."""
        return {}

    def get_working_dir(self, idea_id: str) -> str:
        """Return the working directory for this agent run."""
        return str(self.project_root)

    def _build_deadline_context(self, deadline: datetime) -> str:
        """Build deadline awareness context for the system prompt."""
        now = datetime.now(timezone.utc)
        remaining = (deadline - now).total_seconds()
        minutes = max(0, int(remaining / 60))
        return (
            f"\n\n<deadline>\n"
            f"You have approximately {minutes} minutes remaining to complete this task.\n"
            f"Current time: {now.strftime('%H:%M UTC')}\n"
            f"Deadline: {deadline.strftime('%H:%M UTC')}\n"
            f"Plan your work to fit within this window. If you cannot finish everything,\n"
            f"prioritize saving progress and documenting what remains.\n"
            f"</deadline>"
        )

    def _message_to_dict(self, message) -> dict | None:
        """Convert an SDK message to a serializable dict for the transcript."""
        ts = datetime.now(timezone.utc).isoformat()

        if isinstance(message, AssistantMessage):
            blocks = []
            for block in (message.content or []):
                if isinstance(block, TextBlock):
                    blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    blocks.append({
                        "type": "tool_use",
                        "name": block.name,
                        "input": block.input if isinstance(block.input, (dict, str)) else str(block.input),
                    })
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
                "usage": message.usage if isinstance(getattr(message, "usage", None), dict) else None,
                "timestamp": ts,
            }

        # Catch-all for other message types
        return {
            "role": "unknown",
            "type": type(message).__name__,
            "repr": repr(message)[:1000],
            "timestamp": ts,
        }

    def _save_transcript(self, idea_id: str, transcript: list[dict], prompt: str, system_prompt: str) -> None:
        """Save the full agent transcript to the blackboard."""
        log_dir = self.blackboard.idea_dir(idea_id) / "agent-logs"
        log_dir.mkdir(exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        log_file = log_dir / f"{self.config.name}-{ts}.json"

        log_data = {
            "agent": self.config.name,
            "idea_id": idea_id,
            "model": self.config.model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "system_prompt": system_prompt,
            "user_prompt": prompt,
            "max_turns": self.config.max_turns,
            "max_budget_usd": self.config.max_budget_usd,
            "tools": self.config.tools,
            "transcript": transcript,
        }
        log_file.write_text(json.dumps(log_data, indent=2, default=str))
        logger.info("Saved transcript to %s", log_file)

    async def run(self, idea_id: str, max_turns_override: int | None = None, deadline: datetime | None = None) -> AgentResult:
        """Run this agent against an idea."""
        logger.info("Running agent '%s' on idea '%s'", self.config.name, idea_id)

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
            return await self._run_inner(idea_id, max_turns_override=max_turns_override, deadline=deadline)
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
            if f.is_file() and f.suffix in (".md", ".txt", ".json"):
                parts.append(f"### {f.name}\n{f.read_text()}")
        return "\n\n".join(parts)

    def _get_refinement_context(self, idea_id: str) -> str:
        """Build refinement-mode context for the prompt."""
        status = self.blackboard.get_status(idea_id)
        history = status.get("phase_history", [])
        release_count = sum(1 for entry in history if entry.get("to") == "released")
        iteration_count = status.get("iteration_count", 0)

        return (
            f"\n\n## ⚡ REFINEMENT MODE (cycle #{release_count})\n"
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

    async def _run_inner(self, idea_id: str, max_turns_override: int | None = None, deadline: datetime | None = None) -> AgentResult:
        """Inner run logic, wrapped by activity tracking."""

        # Ensure per-idea knowledge directory exists
        idea_knowledge_dir = self._get_idea_knowledge_dir(idea_id)
        idea_knowledge_dir.mkdir(parents=True, exist_ok=True)

        # Build MCP servers for custom tools
        bb_server = create_blackboard_mcp_server(self.blackboard, idea_id)
        tg_server = create_telegram_mcp_server(self.dispatcher, idea_id)
        ev_server = create_evolution_mcp_server(self.get_knowledge_dir())

        mcp_servers = {
            "blackboard": bb_server,
            "telegram": tg_server,
            "evolution": ev_server,
        }

        # Build the prompt with idea context
        system_prompt = self.config.system_prompt_override or self.get_system_prompt(idea_id)

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
        knowledge_path = self.get_knowledge_dir() / "learnings.md"
        if knowledge_path.exists():
            knowledge_context += (
                f"\n\n## Global Agent Learnings\n{knowledge_path.read_text()}"
            )
        idea_knowledge = self._load_idea_knowledge(idea_id)
        if idea_knowledge:
            knowledge_context += (
                f"\n\n## Your Previous Notes on This Idea\n{idea_knowledge}"
            )

        # Check if this is a refinement run
        is_refining = self._is_refinement_run(idea_id)
        refinement_context = self._get_refinement_context(idea_id) if is_refining else ""

        # Read the idea description
        idea_content = self.blackboard.read_file(idea_id, "idea.md")

        prompt = (
            f"## Idea\n{idea_content}\n"
            f"{knowledge_context}"
            f"{refinement_context}\n\n"
            f"Now execute your role for idea '{idea_id}'. "
            f"Use the blackboard tools to read existing files and write your outputs. "
            f"When done, use set_phase_recommendation to indicate what should happen next."
        )

        subagents = self.get_subagents()

        # Merge env: always unset CLAUDECODE to allow nested sessions,
        # then layer on any per-agent env vars from config
        env = {"CLAUDECODE": ""}
        if self.config.env:
            env.update(self.config.env)

        options = ClaudeAgentOptions(
            cwd=self.get_working_dir(idea_id),
            system_prompt=system_prompt,
            allowed_tools=self.config.tools,
            max_turns=max_turns_override or self.config.max_turns,
            model=self.config.model,
            mcp_servers=mcp_servers,
            permission_mode=self.config.permission_mode,
            env=env,
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
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    # Record every message in the transcript
                    entry = self._message_to_dict(message)
                    if entry:
                        transcript.append(entry)

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
                        self._save_transcript(idea_id, transcript, prompt, system_prompt)
                        return AgentResult(
                            success=True,
                            output="\n".join(output_parts),
                            cost_usd=cost,
                            transcript=transcript,
                        )
        except Exception as e:
            logger.exception("Agent '%s' failed on '%s'", self.config.name, idea_id)
            self._save_transcript(idea_id, transcript, prompt, system_prompt)
            return AgentResult(success=False, error=str(e), transcript=transcript)

        self._save_transcript(idea_id, transcript, prompt, system_prompt)
        return AgentResult(success=True, output="\n".join(output_parts), transcript=transcript)
