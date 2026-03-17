"""Settings routes for editing global prompts and system configuration."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from incubator.config import get_settings
from incubator.web.api.paths import TEMPLATES_DIR

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
        prompts.append({
            "name": agent_dir.name,
            "prompt": prompt_text or "",
            "path": str(prompt_path.relative_to(settings.project_root)),
        })
    return prompts


@router.get("/", response_class=HTMLResponse)
async def settings_view(request: Request):
    global_prompt = _read_global_prompt()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "global_prompt": global_prompt,
        "saved": request.query_params.get("saved"),
    })


@router.post("/global-prompt", response_class=HTMLResponse)
async def save_global_prompt(global_prompt: str = Form(...)):
    path = _global_prompt_path()
    path.write_text(global_prompt)
    return RedirectResponse(url="/settings?saved=global", status_code=303)


@router.post("/agent-prompt/{agent_name}", response_class=HTMLResponse)
async def save_agent_prompt(agent_name: str, prompt: str = Form(...)):
    _write_agent_prompt(agent_name, prompt)
    return RedirectResponse(url=f"/settings?saved={agent_name}", status_code=303)
