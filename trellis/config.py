from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def find_project_root(start: Path = None) -> Path:
    """Walk up from start (default cwd) looking for .trellis (or legacy .incubator) marker."""
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / ".trellis").is_file():
            return current
        if (current / ".incubator").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise FileNotFoundError("Not a Trellis project. Run 'trellis init' first.")


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

    # Worker pool
    pool_size: int = 3
    job_timeout_minutes: int = 60
    producer_interval_seconds: int = 10

    # Iteration / refinement
    max_iterate_per_stage: int = 3
    max_refinement_cycles: int = 1

    # Quality gate (0 = disabled)
    min_quality_score: float = 0.0

    # Projection cache
    projection_url: str = (
        "mem://"  # "mem://" local, "file://trellis.db" persistent, "ws://host:port" k8s
    )

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8000


PROJECT_SETTINGS_FILE = "project_settings.json"

# Cache for get_settings()
_settings_cache: Settings | None = None
_settings_cache_time: float = 0.0
_settings_cache_mtime: float = 0.0
_SETTINGS_TTL = 5.0  # seconds


def _load_project_settings(root: Path) -> dict:
    """Read project_settings.json overlay. Returns {} on missing/invalid."""
    path = root / PROJECT_SETTINGS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_project_settings(settings: dict) -> None:
    """Atomic write of project_settings.json (tempfile + rename)."""
    root = _discover_project_root()
    path = root / PROJECT_SETTINGS_FILE
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(root), suffix=".tmp")
    try:
        with open(tmp_fd, "w") as f:
            json.dump(settings, f, indent=2)
        Path(tmp_path).replace(path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def get_settings() -> Settings:
    """Return effective settings: .env base merged with project_settings.json overlay.

    Cached with 5-second TTL or mtime check for live reload.
    """
    global _settings_cache, _settings_cache_time, _settings_cache_mtime

    now = time.monotonic()
    root = _discover_project_root()
    overlay_path = root / PROJECT_SETTINGS_FILE

    # Check if cache is still valid
    if _settings_cache is not None and (now - _settings_cache_time) < _SETTINGS_TTL:
        # Quick mtime check for invalidation
        try:
            current_mtime = overlay_path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        if current_mtime == _settings_cache_mtime:
            return _settings_cache

    # Build fresh settings
    base = Settings()
    overlay = _load_project_settings(root)
    if overlay:
        base = base.model_copy(update=overlay)

    # Update cache
    try:
        _settings_cache_mtime = overlay_path.stat().st_mtime
    except OSError:
        _settings_cache_mtime = 0.0
    _settings_cache = base
    _settings_cache_time = now
    return base


def _invalidate_settings_cache() -> None:
    """Force next get_settings() call to reload. Used after saving."""
    global _settings_cache, _settings_cache_time
    _settings_cache = None
    _settings_cache_time = 0.0
