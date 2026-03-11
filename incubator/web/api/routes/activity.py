"""Activity tracking routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from incubator.config import get_settings
from incubator.core.activity import ActivityTracker

router = APIRouter()
_templates_dir = str(
    get_settings().project_root / "incubator" / "web" / "frontend" / "templates"
)
templates = Jinja2Templates(directory=_templates_dir)


def _get_tracker() -> ActivityTracker:
    settings = get_settings()
    return ActivityTracker(settings.blackboard_dir.parent / ".activity.json")


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    tracker = _get_tracker()
    tracker.clear_stale()
    running = tracker.get_running()
    return templates.TemplateResponse(
        "activity.html", {"request": request, "running": running}
    )


@router.get("/api/activity")
async def api_activity():
    tracker = _get_tracker()
    tracker.clear_stale()
    return {"running": tracker.get_running()}
