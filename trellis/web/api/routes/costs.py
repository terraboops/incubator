"""Cost tracking routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from trellis.config import get_settings
from trellis.core.blackboard import Blackboard
from trellis.web.api.paths import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
async def costs_view(request: Request):
    bb = Blackboard(get_settings().blackboard_dir)
    ideas = []
    total_cost = 0.0

    for idea_id in bb.list_ideas():
        status = bb.get_status(idea_id)
        cost = status.get("total_cost_usd", 0.0)
        total_cost += cost
        ideas.append({"id": idea_id, "title": status["title"], "cost": cost})

    return templates.TemplateResponse(
        "costs.html",
        {"request": request, "ideas": ideas, "total_cost": total_cost},
    )


@router.get("/summary")
async def api_cost_summary():
    bb = Blackboard(get_settings().blackboard_dir)
    total = 0.0
    per_idea = {}
    for idea_id in bb.list_ideas():
        status = bb.get_status(idea_id)
        cost = status.get("total_cost_usd", 0.0)
        total += cost
        per_idea[idea_id] = cost
    return {"total_usd": total, "per_idea": per_idea}
