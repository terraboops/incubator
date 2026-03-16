"""Pool status and configuration routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from incubator.config import get_settings
from incubator.web.api.paths import TEMPLATES_DIR
from incubator.core.blackboard import Blackboard
from incubator.core.registry import load_registry

router = APIRouter()
settings = get_settings()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _read_pool_state() -> dict:
    """Read the latest pool state snapshot."""
    state_path = settings.project_root / "pool" / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {
        "pool_size": settings.pool_size,
        "queue_depth": 0,
        "workers": [],
        "cadence_trackers": {},
    }


def _compute_idle_reasons(state: dict) -> None:
    """Set an 'idle_reason' field on each idle worker explaining why it's idle."""
    workers = state.get("workers", [])
    if not workers:
        return

    active_ideas = {w.get("idea") for w in workers if w.get("status") == "active"}
    queue_depth = state.get("queue_depth", 0)

    bb = Blackboard(settings.blackboard_dir)
    terminal = {"killed", "paused"}

    total_ideas = 0
    for idea_id in bb.list_ideas():
        status = bb.get_status(idea_id)
        phase = status.get("phase", "submitted")
        if phase in terminal:
            continue
        if phase == "released" and not bb.pending_post_ready(idea_id):
            continue
        total_ideas += 1

    busy_ideas = len(active_ideas)

    for w in workers:
        if w.get("status") != "idle":
            continue

        parts = []
        if queue_depth > 0:
            parts.append(f"{queue_depth} jobs queued but constrained (parallelism/max_concurrent)")
        elif busy_ideas:
            parts.append(f"{busy_ideas} of {total_ideas} ideas being worked on")
        elif total_ideas == 0:
            parts.append("No ideas are ready for processing.")
        else:
            parts.append("No eligible work right now.")

        w["idle_reason"] = ". ".join(parts) + ("" if parts[-1].endswith(".") else ".")


def _normalize_workers(state: dict) -> None:
    """Ensure all workers have a 'status' field for template rendering."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for w in state.get("workers", []):
        if "status" not in w:
            w["status"] = "active" if w.get("role") else "idle"
        if "idea" not in w and "idea_id" in w:
            w["idea"] = w["idea_id"]
        if w.get("status") == "active" and w.get("started_at"):
            try:
                started = datetime.fromisoformat(w["started_at"])
                w["elapsed_seconds"] = (now - started).total_seconds()
            except (ValueError, TypeError):
                pass


@router.get("/", response_class=HTMLResponse)
async def pool_status(request: Request):
    state = _read_pool_state()
    _normalize_workers(state)
    _compute_idle_reasons(state)

    return templates.TemplateResponse("pool.html", {
        "request": request,
        "state": state,
    })


@router.get("/api/state")
async def api_pool_state():
    return _read_pool_state()
