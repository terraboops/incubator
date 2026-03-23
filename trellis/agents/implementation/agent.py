from __future__ import annotations

import os

from claude_agent_sdk import AgentDefinition

from trellis.agents.implementation.prompt import SYSTEM_PROMPT
from trellis.core.agent import BaseAgent


class ImplementationAgent(BaseAgent):
    def get_system_prompt(self, idea_id: str) -> str:
        return SYSTEM_PROMPT

    def get_working_dir(self, idea_id: str) -> str:
        workspace = self.project_root / "workspace" / idea_id
        workspace.mkdir(parents=True, exist_ok=True)
        return str(workspace)

    def get_subagents(self) -> dict[str, AgentDefinition]:
        return {
            "architect": AgentDefinition(
                description="Senior architect for high-level design review",
                prompt=(
                    "You are a senior software architect. Review the proposed architecture "
                    "and provide feedback on structure, patterns, and potential issues. "
                    "You are read-only — do not modify files."
                ),
                tools=["Read", "Glob", "Grep"],
            ),
            "reviewer": AgentDefinition(
                description="Code reviewer for quality and correctness",
                prompt=(
                    "You are a code reviewer. Review the code for bugs, security issues, "
                    "and quality problems. Provide specific, actionable feedback."
                ),
                tools=["Read", "Glob", "Grep"],
            ),
        }
