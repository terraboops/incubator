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

STARVATION_THRESHOLD = 0.5
DEADLINE_WARNING_THRESHOLD = 2


def _read_pool_state() -> dict:
    """Read the latest pool state snapshot."""
    state_path = settings.project_root / "pool" / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {
        "pool_size": settings.pool_size,
        "cycle_time_minutes": settings.cycle_time_minutes,
        "current_window": {"started_at": None, "serviced": [], "remaining_seconds": 0},
        "workers": [],
        "role_health": {},
        "deadline_counts": {},
    }


def _compute_idle_reasons(state: dict) -> None:
    """Set an 'idle_reason' field on each idle worker explaining why it's idle."""
    workers = state.get("workers", [])
    if not workers:
        return

    active_ideas = {w.get("idea") for w in workers if w.get("status") == "active"}
    window = state.get("current_window", {})
    serviced_pairs = window.get("serviced", [])
    remaining = window.get("remaining_seconds", 0)

    # Get the real picture from the blackboard
    bb = Blackboard(settings.blackboard_dir)
    registry = load_registry(settings.registry_path)
    roles = [a.name for a in registry.agents.values() if a.status == "active"]
    terminal = {"killed", "paused"}

    total_ideas = 0
    busy_ideas = len(active_ideas)
    no_work_ideas = []  # ideas with nothing to schedule

    for idea_id in bb.list_ideas():
        status = bb.get_status(idea_id)
        phase = status.get("phase", "submitted")
        if phase in terminal:
            continue
        if phase == "released" and not bb.pending_post_ready(idea_id):
            continue
        total_ideas += 1

        if idea_id in active_ideas:
            continue  # already being worked on

        # Check if this idea has any eligible work
        has_work = False
        if not bb.is_ready(idea_id):
            next_stage = bb.next_stage(idea_id)
            if next_stage and next_stage in roles:
                has_work = True
        else:
            if bb.pending_post_ready(idea_id):
                has_work = True

        if not has_work:
            # Check feedback
            for role in roles:
                if bb.has_pending_feedback(idea_id, role):
                    has_work = True
                    break

        if not has_work:
            title = status.get("title", idea_id)
            no_work_ideas.append(title)

    for w in workers:
        if w.get("status") != "idle":
            continue

        parts = []
        if busy_ideas:
            parts.append(f"{busy_ideas} of {total_ideas} ideas being worked on (1 agent per idea)")
        if no_work_ideas:
            names = ", ".join(no_work_ideas)
            parts.append(f"{names} — no eligible work this cycle")
        if not parts:
            if serviced_pairs and remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                parts.append(f"All work done this cycle. Next cycle in {mins}m {secs}s.")
            elif total_ideas == 0:
                parts.append("No ideas are ready for processing.")
            else:
                parts.append("Waiting for the next cycle.")

        w["idle_reason"] = ". ".join(parts) + ("" if parts[-1].endswith(".") else ".")


def _normalize_workers(state: dict) -> None:
    """Ensure all workers have a 'status' field for template rendering.

    Handles snapshots from before the status field was added — workers
    with a 'role' field but no 'status' are active.
    """
    for w in state.get("workers", []):
        if "status" not in w:
            w["status"] = "active" if w.get("role") else "idle"
        # Normalize 'idea_id' to 'idea' for template (old snapshots use idea_id)
        if "idea" not in w and "idea_id" in w:
            w["idea"] = w["idea_id"]


@router.get("/", response_class=HTMLResponse)
async def pool_status(request: Request):
    state = _read_pool_state()
    _normalize_workers(state)
    _compute_idle_reasons(state)

    # Compute warnings
    starved_roles = []
    for role, health in state.get("role_health", {}).items():
        expected = health.get("expected", 0)
        actual = health.get("actual", 0)
        if expected >= 3 and actual / expected < STARVATION_THRESHOLD:
            starved_roles.append({"role": role, "expected": expected, "actual": actual})

    deadline_warnings = []
    for role, count in state.get("deadline_counts", {}).items():
        if count > DEADLINE_WARNING_THRESHOLD:
            deadline_warnings.append({"role": role, "count": count})

    return templates.TemplateResponse("pool.html", {
        "request": request,
        "state": state,
        "starved_roles": starved_roles,
        "deadline_warnings": deadline_warnings,
    })


@router.get("/api/state")
async def api_pool_state():
    return _read_pool_state()
