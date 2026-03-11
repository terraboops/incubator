from __future__ import annotations

from incubator.agents.ideation.prompt import SYSTEM_PROMPT
from incubator.core.agent import BaseAgent


class IdeationAgent(BaseAgent):
    def get_system_prompt(self, idea_id: str) -> str:
        return SYSTEM_PROMPT
