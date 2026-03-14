from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def find_project_root(start: Path = None) -> Path:
    """Walk up from start (default cwd) looking for .incubator marker."""
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / ".incubator").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise FileNotFoundError(
        "Not an incubator project. Run 'incubator init' first."
    )


def _discover_project_root() -> Path:
    """Try marker-based discovery, fall back to repo root for development."""
    try:
        return find_project_root()
    except FileNotFoundError:
        return Path(__file__).resolve().parent.parent


def _find_env_file() -> str:
    """Resolve .env from project root. Evaluated once at import time."""
    try:
        return str(find_project_root() / ".env")
    except FileNotFoundError:
        return ".env"


_PROJECT_ROOT = _discover_project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_env_file(), env_file_encoding="utf-8", extra="ignore"
    )

    # Paths
    project_root: Path = _PROJECT_ROOT
    blackboard_dir: Path = _PROJECT_ROOT / "blackboard" / "ideas"
    workspace_dir: Path = _PROJECT_ROOT / "workspace"
    registry_path: Path = _PROJECT_ROOT / "registry.yaml"

    # Package paths (not configurable)
    package_root: Path = Path(__file__).resolve().parent
    defaults_dir: Path = Path(__file__).resolve().parent / "defaults"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Budget defaults (USD)
    max_budget_ideation: float = 0.50
    max_budget_implementation: float = 10.00
    max_budget_validation: float = 1.00
    max_budget_release: float = 3.00
    max_budget_watcher: float = 0.10

    # Model tiers
    model_tier_high: str = "claude-sonnet-4-6"
    model_tier_low: str = "claude-haiku-4-5"

    # Watcher cadences
    watcher_competitive_cron: str = "0 */6 * * *"
    watcher_research_cron: str = "0 8 * * *"

    # Worker pool
    pool_size: int = 3
    cycle_time_minutes: int = 30

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8000


def get_settings() -> Settings:
    return Settings()
