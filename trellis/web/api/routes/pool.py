"""Pool status and configuration routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from trellis.config import get_settings
from trellis.web.api.filters import setup_filters
from trellis.web.api.paths import TEMPLATES_DIR
from trellis.core.blackboard import Blackboard

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
setup_filters(templates)


def _read_pool_state() -> dict:
    """Read the latest pool state snapshot."""
    settings = get_settings()
    state_path = settings.project_root / "pool" / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {
        "pool_size": settings.pool_size,
        "queue_depth": 0,
        "workers": [],
        "cadence_trackers": {},
    }


def _compute_idle_reasons(state: dict, request=None) -> None:
    """Set an 'idle_reason' field on each idle worker explaining why it's idle."""
    workers = state.get("workers", [])
    if not workers:
        return

    active_ideas = {w.get("idea") for w in workers if w.get("status") == "active"}
    queue_depth = state.get("queue_depth", 0)

    settings = get_settings()
    bb = Blackboard(settings.blackboard_dir)
    terminal = {"killed", "paused"}

    projection = getattr(request.app.state, "projection", None) if hasattr(request, "app") else None
    total_ideas = 0
    if projection and projection._db:
        metrics = projection.get_metrics()
        if metrics:
            for phase, count in metrics.get("ideas_by_phase", {}).items():
                if phase not in terminal and phase != "released":
                    total_ideas += count
    else:
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


def _enrich_cadence_trackers(state: dict) -> None:
    """Add heartbeat dots and time_left to cadence tracker data."""
    from datetime import datetime, timezone
    from croniter import croniter

    settings = get_settings()
    now = datetime.now(timezone.utc)
    bb = Blackboard(settings.blackboard_dir)

    for role, tracker in state.get("cadence_trackers", {}).items():
        cron_expr = tracker.get("cron", "")
        last_run_at = tracker.get("last_run_at")

        # Compute time_left from cron
        if last_run_at and cron_expr:
            try:
                last_dt = datetime.fromisoformat(last_run_at)
                cron = croniter(cron_expr, last_dt)
                next_run = cron.get_next(datetime)
                remaining = next_run - now
                secs = int(remaining.total_seconds())
                if secs > 0:
                    if secs >= 3600:
                        tracker["time_left"] = f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
                    else:
                        tracker["time_left"] = f"{secs // 60}m"
                else:
                    tracker["time_left"] = "due now"
            except Exception:
                tracker["time_left"] = ""
        else:
            tracker["time_left"] = "never run"

        # Build heartbeat dots from agent logs across all ideas
        # Look at the last N cadence windows and check if the agent ran in each
        n_dots = 12
        if not cron_expr:
            tracker["heartbeat"] = ["ran"] * n_dots
            continue

        try:
            # Walk backwards N intervals from now
            cron_back = croniter(cron_expr, now)
            window_boundaries = [now]
            for _ in range(n_dots):
                prev = cron_back.get_prev(datetime)
                window_boundaries.append(prev)
            window_boundaries.reverse()  # oldest first

            # Collect all run timestamps for this role from agent logs
            run_times = []
            for idea_id in bb.list_ideas():
                log_dir = bb.idea_dir(idea_id) / "agent-logs"
                if not log_dir.is_dir():
                    continue
                for f in log_dir.iterdir():
                    if not f.name.startswith(role):
                        continue
                    try:
                        data = json.loads(f.read_text())
                        ts = data.get("timestamp", "")
                        if ts:
                            run_times.append(datetime.fromisoformat(ts))
                    except Exception:
                        pass

            # For each window, check if any run happened in it
            dots = []
            for i in range(len(window_boundaries) - 1):
                start = window_boundaries[i]
                end = window_boundaries[i + 1]
                ran_in_window = any(start <= t < end for t in run_times)
                if i == len(window_boundaries) - 2:
                    # Current window
                    if ran_in_window:
                        dots.append("ran")
                    elif tracker.get("is_due"):
                        dots.append("missed")
                    else:
                        dots.append("current")
                else:
                    dots.append("ran" if ran_in_window else "missed")

            tracker["heartbeat"] = dots
        except Exception:
            tracker["heartbeat"] = ["ran"] * n_dots


@router.get("/", response_class=HTMLResponse)
async def pool_status(request: Request):
    state = _read_pool_state()
    _normalize_workers(state)
    _compute_idle_reasons(state, request)
    _enrich_cadence_trackers(state)

    return templates.TemplateResponse(
        "pool.html",
        {
            "request": request,
            "state": state,
        },
    )


@router.get("/api/state")
async def api_pool_state():
    return _read_pool_state()
