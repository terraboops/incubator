from pathlib import Path

from incubator.core.registry import AgentConfig, Registry, load_registry


def test_load_registry(tmp_path: Path):
    yaml_content = """
agents:
  - name: test-agent
    description: A test agent
    model: claude-sonnet-4-6
    max_turns: 10
    max_budget_usd: 0.50
    status: active
    tools: [Read, Write]
    phase: ideation
"""
    path = tmp_path / "registry.yaml"
    path.write_text(yaml_content)

    registry = load_registry(path)
    agent = registry.get_agent("test-agent")
    assert agent is not None
    assert agent.model == "claude-sonnet-4-6"
    assert agent.max_turns == 10
    assert agent.tools == ["Read", "Write"]


def test_load_missing_registry(tmp_path: Path):
    registry = load_registry(tmp_path / "missing.yaml")
    assert len(registry.agents) == 0


def test_register_and_save(tmp_path: Path):
    path = tmp_path / "registry.yaml"
    registry = Registry()
    config = AgentConfig(name="new-agent", description="New agent", max_budget_usd=2.0)
    registry.register_agent(config, path)

    reloaded = load_registry(path)
    agent = reloaded.get_agent("new-agent")
    assert agent is not None
    assert agent.max_budget_usd == 2.0


def test_list_active():
    registry = Registry(
        agents={
            "a": AgentConfig(name="a", description="active", status="active"),
            "b": AgentConfig(name="b", description="inactive", status="inactive"),
        }
    )
    active = registry.list_active()
    assert len(active) == 1
    assert active[0].name == "a"
