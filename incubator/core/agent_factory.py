"""Dynamic agent creation at runtime based on idea context."""

from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import AgentDefinition

from incubator.comms.notifications import NotificationDispatcher
from incubator.core.blackboard import Blackboard
from incubator.core.registry import AgentConfig, Registry

logger = logging.getLogger(__name__)


class AgentFactory:
    """Creates specialized agents at runtime based on idea needs."""

    def __init__(
        self,
        registry: Registry,
        blackboard: Blackboard,
        dispatcher: NotificationDispatcher,
        project_root: Path,
    ) -> None:
        self.registry = registry
        self.blackboard = blackboard
        self.dispatcher = dispatcher
        self.project_root = project_root

        # Import agent classes lazily to avoid circular imports
        self._agent_classes: dict[str, type] = {}
        self._register_builtin_agents()

    def _register_builtin_agents(self) -> None:
        from incubator.agents.ideation.agent import IdeationAgent
        from incubator.agents.implementation.agent import ImplementationAgent
        from incubator.agents.validation.agent import ValidationAgent
        from incubator.agents.release.agent import ReleaseAgent

        self._agent_classes["ideation"] = IdeationAgent
        self._agent_classes["implementation"] = ImplementationAgent
        self._agent_classes["validation"] = ValidationAgent
        self._agent_classes["release"] = ReleaseAgent

    def create_agent(self, role: str):
        """Create an agent instance for the given role."""
        config = self.registry.get_agent(role)
        if not config:
            raise ValueError(f"No agent config found for role: {role}")

        agent_class = self._agent_classes.get(role)
        if not agent_class:
            raise ValueError(f"No agent class registered for role: {role}")

        return agent_class(
            config=config,
            blackboard=self.blackboard,
            dispatcher=self.dispatcher,
            project_root=self.project_root,
        )

    def create_custom_agent(
        self, role: str, idea_context: dict
    ) -> AgentDefinition:
        """Create a specialized AgentDefinition at runtime for subagent use."""
        prompt = self._generate_prompt(role, idea_context)
        tools = self._tools_for_role(role)
        model = self._model_for_role(role)

        return AgentDefinition(
            description=f"Specialized {role} agent",
            prompt=prompt,
            tools=tools,
            model=model,
        )

    def register_agent_class(self, role: str, agent_class: type) -> None:
        self._agent_classes[role] = agent_class

    def _generate_prompt(self, role: str, context: dict) -> str:
        idea_title = context.get("title", "Unknown")
        idea_type = context.get("type", "software")
        return (
            f"You are a specialized {role} analyst. "
            f"Analyze the '{idea_title}' ({idea_type}) idea. "
            f"Provide thorough analysis relevant to your specialty."
        )

    def _tools_for_role(self, role: str) -> list[str]:
        config = self.registry.get_agent(role)
        if config:
            return config.tools
        return ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]

    def _model_for_role(self, role: str) -> str:
        config = self.registry.get_agent(role)
        if config:
            return config.model
        return "claude-sonnet-4-6"
