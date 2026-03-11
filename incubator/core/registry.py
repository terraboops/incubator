from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AgentConfig:
    name: str
    description: str
    model: str = "claude-sonnet-4-6"
    max_turns: int = 50
    max_budget_usd: float = 0
    status: str = "active"
    tools: list[str] = field(default_factory=list)
    phase: str | None = None
    cadence: str | None = None  # cron expression for watchers

    # Extended config (maps to ClaudeAgentOptions)
    permission_mode: str = "bypassPermissions"
    thinking: dict | None = None  # e.g. {"type": "adaptive"}
    setting_sources: list[str] | None = None  # e.g. ["project"] to load CLAUDE.md
    env: dict[str, str] | None = None  # environment variables
    system_prompt_override: str | None = None  # override the default prompt.py
    claude_home: str | None = None  # path to .claude/ dir for this agent


@dataclass
class Registry:
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    def get_agent(self, name: str) -> AgentConfig | None:
        return self.agents.get(name)

    def list_active(self) -> list[AgentConfig]:
        return [a for a in self.agents.values() if a.status == "active"]

    def register_agent(self, config: AgentConfig, registry_path: Path) -> None:
        self.agents[config.name] = config
        self.save(registry_path)

    def save(self, path: Path) -> None:
        data = {
            "agents": [
                {k: v for k, v in vars(agent).items() if v is not None}
                for agent in self.agents.values()
            ]
        }
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


_FIELDS = {f.name for f in AgentConfig.__dataclass_fields__.values()}


def load_registry(path: Path) -> Registry:
    if not path.exists():
        return Registry()
    data = yaml.safe_load(path.read_text()) or {}
    agents = {}
    for entry in data.get("agents", []):
        kwargs = {}
        for key, value in entry.items():
            if key in _FIELDS:
                kwargs[key] = value
        if "name" not in kwargs:
            continue
        config = AgentConfig(**kwargs)
        agents[config.name] = config
    return Registry(agents=agents)
