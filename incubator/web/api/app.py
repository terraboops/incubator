"""FastAPI application for the incubator web dashboard."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from incubator.config import get_settings

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


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool_task = None
        pool_manager = None
        if _pool_enabled:
            from incubator.orchestrator.pool import PoolManager
            settings = get_settings()
            pool_manager = PoolManager(settings)
            pool_task = asyncio.create_task(pool_manager.run())
            app.state.pool_manager = pool_manager
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

    app = FastAPI(title="Incubator Dashboard", lifespan=lifespan)

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Register routes
    from incubator.web.api.routes.ideas import router as ideas_router
    from incubator.web.api.routes.agents import router as agents_router
    from incubator.web.api.routes.decisions import router as decisions_router
    from incubator.web.api.routes.costs import router as costs_router
    from incubator.web.api.routes.activity import router as activity_router
    from incubator.web.api.routes.evolution import router as evolution_router
    from incubator.web.api.websocket import router as ws_router

    app.include_router(activity_router, tags=["activity"])
    app.include_router(ideas_router)
    app.include_router(agents_router, prefix="/agents", tags=["agents"])
    app.include_router(decisions_router, prefix="/api/decisions", tags=["decisions"])
    app.include_router(costs_router, prefix="/costs", tags=["costs"])
    app.include_router(evolution_router, prefix="/evolution", tags=["evolution"])
    app.include_router(ws_router)

    return app


app = create_app()
