"""Settings routes for editing global prompts and system configuration."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from trellis.config import (
    get_settings,
    save_project_settings,
    _invalidate_settings_cache,
)
from trellis.web.api.paths import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _global_prompt_path() -> Path:
    return get_settings().project_root / "global-system-prompt.md"


def _agent_prompt_path(agent_name: str) -> Path:
    return get_settings().project_root / "agents" / agent_name / "prompt.py"


def _read_global_prompt() -> str:
    path = _global_prompt_path()
    return path.read_text() if path.exists() else ""


def _read_agent_prompt(agent_name: str) -> str | None:
    """Extract the SYSTEM_PROMPT string from an agent's prompt.py."""
    path = _agent_prompt_path(agent_name)
    if not path.exists():
        return None
    content = path.read_text()
    # Extract the string between triple quotes
    start = content.find('"""\\')
    if start == -1:
        start = content.find('"""')
    if start == -1:
        return content
    start = content.find('"""', start) + 3
    end = content.rfind('"""')
    if end <= start:
        return content
    prompt = content[start:end]
    # Remove leading backslash-newline if present
    if prompt.startswith("\\"):
        prompt = prompt[1:]
    if prompt.startswith("\n"):
        prompt = prompt[1:]
    return prompt


def _write_agent_prompt(agent_name: str, prompt_text: str) -> None:
    """Write the SYSTEM_PROMPT string back to an agent's prompt.py."""
    path = _agent_prompt_path(agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f'SYSTEM_PROMPT = """\\\n{prompt_text}\n"""\n'
    path.write_text(content)


def _gather_agent_prompts() -> list[dict]:
    """Gather all agent prompts for display."""
    settings = get_settings()
    agents_dir = settings.project_root / "agents"
    prompts = []
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith(("_", ".")):
            continue
        prompt_path = agent_dir / "prompt.py"
        if not prompt_path.exists():
            continue
        prompt_text = _read_agent_prompt(agent_dir.name)
        prompts.append(
            {
                "name": agent_dir.name,
                "prompt": prompt_text or "",
                "path": str(prompt_path.relative_to(settings.project_root)),
            }
        )
    return prompts


def _values_path() -> Path:
    return get_settings().project_root / "values.md"


def _read_values() -> str:
    path = _values_path()
    return path.read_text() if path.exists() else ""


@router.get("/", response_class=HTMLResponse)
async def settings_view(request: Request):
    settings = get_settings()
    global_prompt = _read_global_prompt()
    values = _read_values()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "global_prompt": global_prompt,
            "values": values,
            "settings": settings,
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/global-prompt", response_class=HTMLResponse)
async def save_global_prompt(global_prompt: str = Form(...)):
    path = _global_prompt_path()
    path.write_text(global_prompt)
    return RedirectResponse(url="/settings?saved=global", status_code=303)


@router.post("/values", response_class=HTMLResponse)
async def save_values(values: str = Form(...)):
    path = _values_path()
    path.write_text(values)
    return RedirectResponse(url="/settings?saved=values", status_code=303)


@router.post("/system")
async def save_system_settings(
    request: Request,
    pool_size: int = Form(...),
    job_timeout_minutes: int = Form(...),
    producer_interval_seconds: int = Form(...),
    max_iterate_per_stage: int = Form(...),
    max_refinement_cycles: int = Form(...),
    min_quality_score: float = Form(...),
    max_budget_ideation: float = Form(...),
    max_budget_implementation: float = Form(...),
    max_budget_validation: float = Form(...),
    max_budget_release: float = Form(...),
    max_budget_watcher: float = Form(...),
    model_tier_high: str = Form(...),
    model_tier_low: str = Form(...),
):
    # Validate ranges
    pool_size = max(1, min(20, pool_size))
    job_timeout_minutes = max(1, job_timeout_minutes)
    producer_interval_seconds = max(1, producer_interval_seconds)
    max_iterate_per_stage = max(1, min(20, max_iterate_per_stage))
    max_refinement_cycles = max(0, max_refinement_cycles)
    min_quality_score = max(0.0, min(10.0, min_quality_score))
    max_budget_ideation = max(0.0, max_budget_ideation)
    max_budget_implementation = max(0.0, max_budget_implementation)
    max_budget_validation = max(0.0, max_budget_validation)
    max_budget_release = max(0.0, max_budget_release)
    max_budget_watcher = max(0.0, max_budget_watcher)

    old_settings = get_settings()

    overlay = {
        "pool_size": pool_size,
        "job_timeout_minutes": job_timeout_minutes,
        "producer_interval_seconds": producer_interval_seconds,
        "max_iterate_per_stage": max_iterate_per_stage,
        "max_refinement_cycles": max_refinement_cycles,
        "min_quality_score": min_quality_score,
        "max_budget_ideation": max_budget_ideation,
        "max_budget_implementation": max_budget_implementation,
        "max_budget_validation": max_budget_validation,
        "max_budget_release": max_budget_release,
        "max_budget_watcher": max_budget_watcher,
        "model_tier_high": model_tier_high.strip(),
        "model_tier_low": model_tier_low.strip(),
    }

    save_project_settings(overlay)
    _invalidate_settings_cache()

    new_settings = get_settings()
    if new_settings.pool_size != old_settings.pool_size:
        from trellis.web.api.app import restart_pool

        asyncio.create_task(restart_pool(request.app))

    return RedirectResponse(url="/settings?saved=system", status_code=303)


@router.get("/api/settings")
async def api_settings():
    """JSON API for current effective settings."""
    settings = get_settings()
    return JSONResponse(
        {
            "pool_size": settings.pool_size,
            "job_timeout_minutes": settings.job_timeout_minutes,
            "producer_interval_seconds": settings.producer_interval_seconds,
            "max_iterate_per_stage": settings.max_iterate_per_stage,
            "max_refinement_cycles": settings.max_refinement_cycles,
            "min_quality_score": settings.min_quality_score,
            "max_budget_ideation": settings.max_budget_ideation,
            "max_budget_implementation": settings.max_budget_implementation,
            "max_budget_validation": settings.max_budget_validation,
            "max_budget_release": settings.max_budget_release,
            "max_budget_watcher": settings.max_budget_watcher,
            "model_tier_high": settings.model_tier_high,
            "model_tier_low": settings.model_tier_low,
        }
    )


@router.post("/agent-prompt/{agent_name}", response_class=HTMLResponse)
async def save_agent_prompt(agent_name: str, prompt: str = Form(...)):
    _write_agent_prompt(agent_name, prompt)
    return RedirectResponse(url=f"/settings?saved={agent_name}", status_code=303)
