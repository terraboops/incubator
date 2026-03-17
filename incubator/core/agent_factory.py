"""Dynamic agent creation at runtime based on idea context."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from claude_agent_sdk import AgentDefinition

from incubator.comms.notifications import NotificationDispatcher
from incubator.core.agent import BaseAgent
from incubator.core.blackboard import Blackboard
from incubator.core.registry import AgentConfig, Registry

logger = logging.getLogger(__name__)


class GenericAgent(BaseAgent):
    """Concrete agent that loads its system prompt from a prompt.py file."""

    def __init__(self, *args, system_prompt: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._system_prompt = system_prompt

    def get_system_prompt(self, idea_id: str) -> str:
        return self._system_prompt


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

    def _find_system_prompt(self, role: str) -> str | None:
        """Try to load a SYSTEM_PROMPT from prompt.py in project or defaults dirs."""
        # Search paths: project agents dir (multiple possible locations), then defaults
        search_dirs = [
            self.project_root / "agents" / role,
            self.project_root / "agents" / "watchers",  # competitive.py, research.py
            Path(__file__).parent.parent / "defaults" / "agents" / role,
        ]

        # Try prompt.py first, then role-specific filenames for watchers dir
        filenames = ["prompt"]
        role_to_alt = {
            "competitive-watcher": "competitive",
            "research-watcher": "research",
        }
        if role in role_to_alt:
            filenames.append(role_to_alt[role])

        for search_dir in search_dirs:
          for filename in filenames:
            prompt_file = search_dir / f"{filename}.py"
            if prompt_file.exists():
                # Load the module and extract SYSTEM_PROMPT
                spec = importlib.util.spec_from_file_location(f"_prompt_{role}", prompt_file)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    try:
                        spec.loader.exec_module(mod)
                        if hasattr(mod, "SYSTEM_PROMPT"):
                            return mod.SYSTEM_PROMPT
                    except Exception as e:
                        logger.warning("Failed to load prompt from %s: %s", prompt_file, e)

        return None

    def create_agent(self, role: str):
        """Create an agent instance for the given role."""
        config = self.registry.get_agent(role)
        if not config:
            raise ValueError(f"No agent config found for role: {role}")

        agent_class = self._agent_classes.get(role)
        if agent_class:
            return agent_class(
                config=config,
                blackboard=self.blackboard,
                dispatcher=self.dispatcher,
                project_root=self.project_root,
            )

        # No dedicated class — build a GenericAgent with a discovered prompt
        system_prompt = self._find_system_prompt(role)
        if not system_prompt:
            raise ValueError(
                f"No agent class or prompt.py with SYSTEM_PROMPT found for role '{role}'. "
                f"Create agents/{role}/prompt.py with a SYSTEM_PROMPT constant."
            )

        return GenericAgent(
            config=config,
            blackboard=self.blackboard,
            dispatcher=self.dispatcher,
            project_root=self.project_root,
            system_prompt=system_prompt,
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
