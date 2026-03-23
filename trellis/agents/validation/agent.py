from __future__ import annotations

from trellis.agents.validation.prompt import SYSTEM_PROMPT
from trellis.core.agent import BaseAgent


class ValidationAgent(BaseAgent):
    def get_system_prompt(self, idea_id: str) -> str:
        return SYSTEM_PROMPT

    def get_working_dir(self, idea_id: str) -> str:
        workspace = self.project_root / "workspace" / idea_id
        return str(workspace) if workspace.exists() else str(self.project_root)
