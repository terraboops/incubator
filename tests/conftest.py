"""Shared fixtures for Trellis tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.config import Settings
from trellis.core.blackboard import Blackboard


def pytest_addoption(parser):
    parser.addoption(
        "--browser", action="store_true", default=False, help="Run Playwright browser tests"
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--browser"):
        skip_browser = pytest.mark.skip(reason="Need --browser flag to run Playwright tests")
        for item in items:
            if "browser" in item.keywords:
                item.add_marker(skip_browser)


@pytest.fixture
def trellis_project(tmp_path: Path) -> Path:
    """Scaffold a minimal Trellis project in tmp_path for E2E tests."""
    # Marker
    (tmp_path / ".trellis").write_text('{"version": "0.2.0"}')

    # Blackboard dirs
    ideas_dir = tmp_path / "blackboard" / "ideas"
    template_dir = ideas_dir / "_template"
    template_dir.mkdir(parents=True)
    (template_dir / ".gitkeep").write_text("")

    # __all__ dir for global agents
    all_dir = ideas_dir / "__all__"
    all_dir.mkdir()
    (all_dir / ".gitkeep").write_text("")

    # Pool dir
    (tmp_path / "pool").mkdir()

    # Workspace
    (tmp_path / "workspace").mkdir()

    # Agents dir with minimal prompt.py files
    for role in ("ideation", "implementation", "validation", "release"):
        agent_dir = tmp_path / "agents" / role
        agent_dir.mkdir(parents=True)
        (agent_dir / "prompt.py").write_text(
            f'SYSTEM_PROMPT = "You are the {role} agent. Do your job."\n'
        )
        # .claude dir for agent config
        claude_dir = agent_dir / ".claude"
        claude_dir.mkdir()

    # Pipeline templates
    pipeline_dir = tmp_path / "pipeline-templates"
    pipeline_dir.mkdir()
    (pipeline_dir / "default.yaml").write_text(
        "agents: [ideation, implementation, validation, release]\n"
        "post_ready: []\n"
        "parallel_groups:\n"
        "  - [ideation, implementation, validation, release]\n"
        "gating:\n"
        "  default: auto\n"
        "  overrides: {}\n"
    )

    # Registry — agents is a list, not a dict (see registry.py load_registry)
    registry_content = {
        "agents": [
            {
                "name": "ideation",
                "description": "Market research",
                "model": "claude-sonnet-4-6",
                "max_turns": 10,
                "max_budget_usd": 0.50,
                "max_concurrent": 1,
                "tools": ["Read", "Write"],
                "phase": "ideation",
                "status": "active",
            },
            {
                "name": "implementation",
                "description": "Build MVP",
                "model": "claude-sonnet-4-6",
                "max_turns": 10,
                "max_budget_usd": 10.00,
                "max_concurrent": 1,
                "tools": ["Read", "Write"],
                "phase": "implementation",
                "status": "active",
            },
            {
                "name": "validation",
                "description": "QA testing",
                "model": "claude-sonnet-4-6",
                "max_turns": 10,
                "max_budget_usd": 1.00,
                "max_concurrent": 1,
                "tools": ["Read", "Write"],
                "phase": "validation",
                "status": "active",
            },
            {
                "name": "release",
                "description": "Launch prep",
                "model": "claude-sonnet-4-6",
                "max_turns": 10,
                "max_budget_usd": 3.00,
                "max_concurrent": 1,
                "tools": ["Read", "Write"],
                "phase": "release",
                "status": "active",
            },
        ]
    }

    import yaml

    (tmp_path / "registry.yaml").write_text(yaml.dump(registry_content, default_flow_style=False))

    return tmp_path


@pytest.fixture
def trellis_settings(trellis_project: Path) -> Settings:
    """Create Settings pointing at the test project."""
    return Settings(
        project_root=trellis_project,
        blackboard_dir=trellis_project / "blackboard" / "ideas",
        workspace_dir=trellis_project / "workspace",
        registry_path=trellis_project / "registry.yaml",
        pool_size=1,
        job_timeout_minutes=1,
        producer_interval_seconds=0,
        max_refinement_cycles=1,
        min_quality_score=0.0,
    )


@pytest.fixture
def blackboard(trellis_settings: Settings) -> Blackboard:
    """Create a Blackboard instance for the test project."""
    return Blackboard(trellis_settings.blackboard_dir)
