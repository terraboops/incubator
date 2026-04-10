"""Health and Prometheus metrics endpoints.

/healthz  — k8s liveness probe  (is the process alive?)
/readyz   — k8s readiness probe (can it serve traffic?)
/metrics  — Prometheus text exposition format
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from trellis.config import get_settings

router = APIRouter()

# Record startup time once at import
_START_TIME = time.time()


# ── Liveness ──────────────────────────────────────────────────────────────────


@router.get("/healthz")
async def healthz():
    """k8s liveness probe — returns 200 as long as the process is running."""
    return JSONResponse({"status": "ok"})


# ── Readiness ─────────────────────────────────────────────────────────────────


@router.get("/readyz")
async def readyz():
    """k8s readiness probe — returns 503 if the blackboard is unreadable."""
    checks: dict[str, str] = {}

    # Check blackboard directory is accessible
    try:
        settings = get_settings()
        bb_dir = settings.blackboard_dir
        if not bb_dir.is_dir():
            checks["blackboard"] = "not_found"
        else:
            list(bb_dir.iterdir())  # verify we can actually read it
            checks["blackboard"] = "ok"
    except Exception as e:
        checks["blackboard"] = f"error: {e}"

    # Check pool state file is readable (non-fatal if missing — pool may not be running)
    try:
        state_path = get_settings().project_root / "pool" / "state.json"
        if state_path.exists():
            json.loads(state_path.read_text())
            checks["pool_state"] = "ok"
        else:
            checks["pool_state"] = "no_state_file"
    except Exception as e:
        checks["pool_state"] = f"error: {e}"

    healthy = all(v in ("ok", "no_state_file") for v in checks.values())
    code = 200 if healthy else 503
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks}, status_code=code
    )


# ── Prometheus metrics ────────────────────────────────────────────────────────


def _prom_lines(name: str, help_text: str, type_: str, samples: list[tuple]) -> list[str]:
    """Build Prometheus exposition lines for one metric family."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {type_}"]
    for labels, value in samples:
        if value is None:
            continue
        label_str = ""
        if labels:
            pairs = ",".join(f'{k}="{v}"' for k, v in labels.items())
            label_str = f"{{{pairs}}}"
        lines.append(f"{name}{label_str} {float(value)}")
    return lines


@router.get("/metrics", response_class=Response)
async def metrics():
    """Prometheus text exposition format metrics."""
    settings = get_settings()

    from trellis.core.blackboard import Blackboard
    from trellis.core.activity import ActivityTracker

    bb = Blackboard(settings.blackboard_dir)
    all_lines: list[str] = []

    # ── Process uptime ──
    all_lines += _prom_lines(
        "trellis_up",
        "1 if the trellis server is running.",
        "gauge",
        [(None, 1)],
    )
    all_lines += _prom_lines(
        "trellis_start_time_seconds",
        "Unix timestamp of server start.",
        "gauge",
        [(None, _START_TIME)],
    )

    # ── Ideas by phase ──
    phase_counts: dict[str, int] = {}
    total_cost = 0.0
    per_idea_cost: list[tuple] = []
    per_idea_iters: list[tuple] = []

    sandbox_failure_total = 0

    try:
        idea_ids = bb.list_ideas()
        for idea_id in idea_ids:
            try:
                st = bb.get_status(idea_id)
            except Exception:
                continue
            phase = st.get("phase", "unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            cost = st.get("total_cost_usd", 0.0) or 0.0
            total_cost += cost
            per_idea_cost.append(({"idea": idea_id}, cost))
            per_idea_iters.append(({"idea": idea_id}, st.get("iteration_count", 0)))
            sandbox_failure_total += st.get("sandbox_failure_count", 0)
    except Exception:
        idea_ids = []

    all_lines += _prom_lines(
        "trellis_ideas_by_phase_total",
        "Number of ideas in each pipeline phase.",
        "gauge",
        [({"phase": phase}, count) for phase, count in sorted(phase_counts.items())],
    )
    all_lines += _prom_lines(
        "trellis_ideas_total",
        "Total number of ideas.",
        "gauge",
        [(None, len(idea_ids))],
    )
    all_lines += _prom_lines(
        "trellis_ideas_cost_usd_total",
        "Total USD spend across all ideas.",
        "gauge",
        [(None, total_cost)],
    )
    all_lines += _prom_lines(
        "trellis_idea_cost_usd",
        "Total USD spend per idea.",
        "gauge",
        per_idea_cost,
    )
    all_lines += _prom_lines(
        "trellis_idea_iteration_count",
        "Number of refinement iterations per idea.",
        "gauge",
        per_idea_iters,
    )
    all_lines += _prom_lines(
        "trellis_sandbox_failure_total",
        "Total sandbox failures across all agent runs.",
        "counter",
        [(None, sandbox_failure_total)],
    )

    # ── Pool state ──
    pool_size = 0
    active_workers = 0
    idle_workers = 0
    queue_depth = 0
    cadence_samples: list[tuple] = []
    cadence_due_samples: list[tuple] = []

    try:
        state_path = settings.project_root / "pool" / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            pool_size = state.get("pool_size", 0)
            queue_depth = state.get("queue_depth", 0)
            for w in state.get("workers", []):
                if w.get("status") == "active":
                    active_workers += 1
                else:
                    idle_workers += 1
            for agent, tracker in state.get("cadence_trackers", {}).items():
                last_run = tracker.get("last_run_at")
                if last_run:
                    try:
                        ts = datetime.fromisoformat(last_run).timestamp()
                        cadence_samples.append(({"agent": agent}, ts))
                    except ValueError:
                        pass
                cadence_due_samples.append(({"agent": agent}, 1 if tracker.get("is_due") else 0))
    except Exception:
        pass

    all_lines += _prom_lines(
        "trellis_pool_size",
        "Configured worker pool size.",
        "gauge",
        [(None, pool_size)],
    )
    all_lines += _prom_lines(
        "trellis_pool_workers_active",
        "Number of currently active workers.",
        "gauge",
        [(None, active_workers)],
    )
    all_lines += _prom_lines(
        "trellis_pool_workers_idle",
        "Number of currently idle workers.",
        "gauge",
        [(None, idle_workers)],
    )
    all_lines += _prom_lines(
        "trellis_pool_queue_depth",
        "Number of jobs waiting in the queue.",
        "gauge",
        [(None, queue_depth)],
    )
    all_lines += _prom_lines(
        "trellis_cadence_last_run_timestamp_seconds",
        "Unix timestamp of the last cadence agent run.",
        "gauge",
        cadence_samples,
    )
    all_lines += _prom_lines(
        "trellis_cadence_agent_due",
        "1 if the cadence agent is currently due to run.",
        "gauge",
        cadence_due_samples,
    )

    # ── Activity (currently running agents) ──
    try:
        tracker = ActivityTracker(settings.blackboard_dir.parent / ".activity.json")
        tracker.clear_stale()
        running = tracker.get_running()
        running_count = len(running)
    except Exception:
        running_count = 0

    all_lines += _prom_lines(
        "trellis_agents_running",
        "Number of agent runs currently in progress.",
        "gauge",
        [(None, running_count)],
    )

    body = "\n".join(all_lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
