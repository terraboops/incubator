"""FastAPI application for the Trellis web dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env into os.environ — the project .env is authoritative for
# server-scoped config. override=True is required because when running
# inside a nono sandbox, the parent process has its own CLAUDE_CONFIG_DIR
# pointing at the operator's personal config. The project .env specifies
# the correct auth account (e.g., autonav). Without override, agents
# copy OAuth tokens from the wrong account, causing 401 errors.
from dotenv import load_dotenv
from trellis.config import find_project_root

try:
    load_dotenv(find_project_root() / ".env", override=True)
except FileNotFoundError:
    pass

# Allow Agent SDK calls when running inside a Claude Code session
os.environ.pop("CLAUDECODE", None)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from trellis.config import get_settings

# Logging is configured by the CLI (trellis/log_setup.py) before the app starts.
# When the app module is imported directly (e.g. in tests), fall back to basicConfig
# so there is always at least one handler.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = WEB_DIR / "frontend" / "templates"
STATIC_DIR = WEB_DIR / "frontend" / "static"

_pool_enabled = False


def set_pool_enabled(enabled: bool) -> None:
    """Set whether the pool should start with the app."""
    global _pool_enabled
    _pool_enabled = enabled


def _pool_enabled_flag() -> bool:
    """Expose the flag for testing."""
    return _pool_enabled


async def restart_pool(app: FastAPI) -> None:
    """Gracefully restart the worker pool with fresh settings.

    Called when pool_size changes via the settings page.
    """
    pool_manager = getattr(app.state, "pool_manager", None)
    pool_task = getattr(app.state, "pool_task", None)

    # Stop existing pool
    if pool_manager:
        pool_manager.stop()
        if pool_task and not pool_task.done():
            pool_task.cancel()
            try:
                await pool_task
            except asyncio.CancelledError:
                pass
        logger.info("Worker pool stopped for restart")

    # Start new pool with fresh settings
    from trellis.orchestrator.pool import PoolManager

    settings = get_settings()
    new_manager = PoolManager(settings)
    new_task = asyncio.create_task(
        _resilient_pool(app, new_manager, settings),
        name="pool-supervisor",
    )
    app.state.pool_manager = new_manager
    app.state.pool_task = new_task
    logger.info("Worker pool restarted with pool_size=%d", settings.pool_size)


async def _resilient_pool(app: "FastAPI", pool_manager, settings) -> None:
    """Run the pool with automatic restart on unexpected crashes.

    Tracks restart frequency to avoid infinite restart loops — if the pool
    crashes more than MAX_RAPID_RESTARTS times within RAPID_RESTART_WINDOW,
    we stop retrying and log an error.
    """
    from trellis.orchestrator.pool import (
        PoolManager,
        POOL_RESTART_DELAY_SECONDS,
        MAX_RAPID_RESTARTS,
        RAPID_RESTART_WINDOW_SECONDS,
    )
    import time

    restart_times: list[float] = []

    while True:
        try:
            await pool_manager.run()
            # Clean exit (e.g., stop() was called) — don't restart
            logger.info("Pool exited cleanly")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pool crashed unexpectedly — will attempt restart")

            now = time.monotonic()
            restart_times = [t for t in restart_times if now - t < RAPID_RESTART_WINDOW_SECONDS]
            restart_times.append(now)

            if len(restart_times) >= MAX_RAPID_RESTARTS:
                logger.error(
                    "Pool crashed %d times in %ds — giving up. Check logs and restart the server.",
                    MAX_RAPID_RESTARTS,
                    RAPID_RESTART_WINDOW_SECONDS,
                )
                return

            await asyncio.sleep(POOL_RESTART_DELAY_SECONDS)
            pool_manager = PoolManager(settings)
            app.state.pool_manager = pool_manager
            logger.info("Restarting pool (attempt %d)", len(restart_times))


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool_task = None
        pool_manager = None
        if _pool_enabled:
            from trellis.orchestrator.pool import PoolManager

            settings = get_settings()
            pool_manager = PoolManager(settings)
            pool_task = asyncio.create_task(
                _resilient_pool(app, pool_manager, settings),
                name="pool-supervisor",
            )
            app.state.pool_manager = pool_manager
            app.state.pool_task = pool_task
            logger.info("Worker pool started")
        yield
        if pool_manager:
            pool_manager.stop()
            if pool_task and not pool_task.done():
                pool_task.cancel()
                try:
                    await pool_task
                except asyncio.CancelledError:
                    pass
            logger.info("Worker pool stopped")

    app = FastAPI(title="Trellis Dashboard", lifespan=lifespan)

    # OTEL instrumentation (no-op if not configured)
    from trellis.otel import instrument_fastapi

    instrument_fastapi(app)

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Register routes
    from trellis.web.api.routes.ideas import router as ideas_router
    from trellis.web.api.routes.agents import router as agents_router
    from trellis.web.api.routes.decisions import router as decisions_router
    from trellis.web.api.routes.costs import router as costs_router
    from trellis.web.api.routes.activity import router as activity_router
    from trellis.web.api.routes.evolution import router as evolution_router
    from trellis.web.api.routes.pool import router as pool_router
    from trellis.web.api.routes.settings import router as settings_router
    from trellis.web.api.routes.migrations import router as migrations_router
    from trellis.web.api.routes.pipelines import router as pipelines_router
    from trellis.web.api.routes.health import router as health_router
    from trellis.web.api.websocket import router as ws_router

    app.include_router(health_router, tags=["health"])
    app.include_router(activity_router, tags=["activity"])
    app.include_router(ideas_router)
    app.include_router(agents_router, prefix="/agents", tags=["agents"])
    app.include_router(decisions_router, prefix="/api/decisions", tags=["decisions"])
    app.include_router(costs_router, prefix="/costs", tags=["costs"])
    app.include_router(evolution_router, prefix="/evolution", tags=["evolution"])
    app.include_router(pool_router, prefix="/pool", tags=["pool"])
    app.include_router(settings_router, prefix="/settings", tags=["settings"])
    app.include_router(pipelines_router, prefix="/pipelines", tags=["pipelines"])
    app.include_router(migrations_router, prefix="/api/migrations", tags=["migrations"])
    app.include_router(ws_router)

    return app


app = create_app()
