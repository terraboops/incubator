"""Agent status and management routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from incubator.config import get_settings
from incubator.core.blackboard import Blackboard
from incubator.core.registry import AgentConfig, load_registry
from incubator.web.api.paths import TEMPLATES_DIR
from incubator.web.api.routes.settings import _read_agent_prompt, _write_agent_prompt

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Register shared filters
_CADENCE_PATTERNS = {
    "0 */6 * * *": "every 6h",
    "0 */4 * * *": "every 4h",
    "0 */12 * * *": "every 12h",
    "0 8 * * *": "daily at 8am",
    "0 0 * * *": "daily at midnight",
    "*/30 * * * *": "every 30min",
}
templates.env.filters["cadence_label"] = lambda cron: _CADENCE_PATTERNS.get(cron, cron)

# Shared constants for templates
ALL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "AskUserQuestion"]
MODELS = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]
PERMISSION_MODES = ["bypassPermissions", "acceptEdits", "default", "plan", "dontAsk"]


def _agent_view_data(agent: AgentConfig, settings) -> dict:
    data = vars(agent).copy()
    knowledge_dir = (
        settings.project_root / "agents"
        / (agent.phase or agent.name) / "knowledge"
    )
    learnings_path = knowledge_dir / "learnings.md"
    data["knowledge_size"] = learnings_path.stat().st_size if learnings_path.exists() else 0

    claude_md = ""
    if agent.claude_home:
        claude_md_path = settings.project_root / agent.claude_home / "CLAUDE.md"
        if claude_md_path.exists():
            claude_md = claude_md_path.read_text()
    data["claude_md"] = claude_md

    # Load prompt.py content
    prompt_py = _read_agent_prompt(agent.phase or agent.name)
    if prompt_py is None:
        prompt_py = _read_agent_prompt(agent.name)
    data["prompt_py"] = prompt_py or ""
    prompt_path = (
        settings.project_root / "incubator" / "agents"
        / (agent.phase or agent.name) / "prompt.py"
    )
    data["prompt_path"] = str(prompt_path.relative_to(settings.project_root)) if prompt_path.exists() else ""
    return data


def _apply_form_to_config(config: AgentConfig, *, description: str, model: str,
                           max_turns: int, max_budget_usd: float, max_concurrent: int,
                           permission_mode: str,
                           tools: str, thinking_type: str, status: str,
                           phase: str, cadence: str, setting_sources: str,
                           system_prompt_override: str, env_text: str) -> None:
    config.description = description
    config.model = model
    config.max_turns = max_turns
    config.max_budget_usd = max_budget_usd
    config.max_concurrent = max_concurrent
    config.permission_mode = permission_mode
    config.status = status
    config.phase = phase.strip() or None
    config.cadence = cadence.strip() or None
    config.tools = [t.strip() for t in tools.split(",") if t.strip()] if tools.strip() else []
    config.thinking = {"type": thinking_type} if thinking_type else None
    config.setting_sources = [s.strip() for s in setting_sources.split(",") if s.strip()] if setting_sources.strip() else None
    config.system_prompt_override = system_prompt_override.strip() or None

    # Parse env as key=value lines
    if env_text.strip():
        env = {}
        for line in env_text.strip().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        config.env = env or None
    else:
        config.env = None


def _save_claude_md(config: AgentConfig, claude_md: str, settings) -> None:
    if config.claude_home and claude_md is not None:
        claude_md_path = settings.project_root / config.claude_home / "CLAUDE.md"
        claude_md_path.parent.mkdir(parents=True, exist_ok=True)
        claude_md_path.write_text(claude_md)


def _template_ctx(agent: dict, is_new: bool = False) -> dict:
    return {
        "agent": agent,
        "is_new": is_new,
        "all_tools": ALL_TOOLS,
        "models": MODELS,
        "permission_modes": PERMISSION_MODES,
    }


# --- Routes ---

@router.get("/", response_class=HTMLResponse)
async def agents_view(request: Request):
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    agents = [_agent_view_data(a, settings) for a in registry.agents.values()]
    return templates.TemplateResponse("agents.html", {"request": request, "agents": agents})


@router.get("/new", response_class=HTMLResponse)
async def new_agent_form(request: Request):
    blank = {
        "name": "", "description": "", "model": "claude-sonnet-4-6",
        "max_turns": 50, "max_budget_usd": 1.0, "status": "active",
        "tools": [], "phase": "", "cadence": "", "permission_mode": "bypassPermissions",
        "thinking": {"type": "adaptive"}, "setting_sources": ["project"],
        "env": None, "system_prompt_override": "", "claude_home": "",
        "claude_md": "", "knowledge_size": 0,
    }
    ctx = _template_ctx(blank, is_new=True)
    ctx["request"] = request
    ctx["agent_logs"] = []
    ctx["associated_ideas"] = {}
    return templates.TemplateResponse("agent_detail.html", ctx)


@router.post("/new", response_class=HTMLResponse)
async def create_agent(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    model: str = Form("claude-sonnet-4-6"),
    max_turns: int = Form(50),
    max_budget_usd: float = Form(1.0),
    max_concurrent: int = Form(1),
    permission_mode: str = Form("bypassPermissions"),
    tools: str = Form("Read, Write, Edit, Bash, Glob, Grep"),
    thinking_type: str = Form("adaptive"),
    status: str = Form("active"),
    phase: str = Form(""),
    cadence: str = Form(""),
    setting_sources: str = Form("project"),
    system_prompt_override: str = Form(""),
    env_text: str = Form(""),
    claude_md: str = Form(""),
):
    settings = get_settings()
    registry = load_registry(settings.registry_path)

    # Slugify name
    slug = name.strip().lower().replace(" ", "-")
    if registry.get_agent(slug):
        # Already exists — redirect to edit
        return RedirectResponse(url=f"/agents/{slug}", status_code=303)

    config = AgentConfig(name=slug, description=description)
    config.claude_home = f"agents/{slug}/.claude"

    _apply_form_to_config(
        config, description=description, model=model, max_turns=max_turns,
        max_budget_usd=max_budget_usd, max_concurrent=max_concurrent,
        permission_mode=permission_mode,
        tools=tools, thinking_type=thinking_type, status=status,
        phase=phase, cadence=cadence, setting_sources=setting_sources,
        system_prompt_override=system_prompt_override, env_text=env_text,
    )

    registry.register_agent(config, settings.registry_path)
    _save_claude_md(config, claude_md, settings)

    return RedirectResponse(url=f"/agents/{slug}", status_code=303)


@router.get("/{agent_name}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_name: str):
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    config = registry.get_agent(agent_name)
    if not config:
        return HTMLResponse("Agent not found", status_code=404)

    agent = _agent_view_data(config, settings)

    # Deadline pressure from pool state
    deadline_count = 0
    pool_state_path = settings.project_root / "pool" / "state.json"
    if pool_state_path.exists():
        try:
            pool_state = json.loads(pool_state_path.read_text())
            deadline_count = pool_state.get("deadline_counts", {}).get(agent_name, 0)
        except (json.JSONDecodeError, OSError):
            pass

    # Gather logs for this agent across all ideas
    bb = Blackboard(settings.blackboard_dir)
    agent_logs = []
    associated_ideas = {}
    for idea_id in bb.list_ideas():
        log_dir = bb.idea_dir(idea_id) / "agent-logs"
        if not log_dir.is_dir():
            continue
        idea_status = bb.get_status(idea_id)
        for f in sorted(log_dir.iterdir(), reverse=True):
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            if data.get("agent") != agent_name:
                continue
            associated_ideas[idea_id] = idea_status.get("title", idea_id)
            agent_logs.append({
                "filename": f.name,
                "idea_id": idea_id,
                "idea_title": idea_status.get("title", idea_id),
                "timestamp": data.get("timestamp", ""),
                "model": data.get("model", ""),
                "transcript_len": len(data.get("transcript", [])),
                "run_status": data.get("run_status", ""),
            })

    # Sort logs by timestamp descending
    agent_logs.sort(key=lambda x: x["timestamp"], reverse=True)

    ctx = _template_ctx(agent)
    ctx["request"] = request
    ctx["agent_logs"] = agent_logs
    ctx["associated_ideas"] = associated_ideas
    ctx["deadline_count"] = deadline_count
    return templates.TemplateResponse("agent_detail.html", ctx)


@router.post("/{agent_name}", response_class=HTMLResponse)
async def agent_update(
    request: Request,
    agent_name: str,
    description: str = Form(""),
    model: str = Form(...),
    max_turns: int = Form(...),
    max_budget_usd: float = Form(...),
    max_concurrent: int = Form(1),
    permission_mode: str = Form(...),
    tools: str = Form(""),
    thinking_type: str = Form(""),
    status: str = Form("active"),
    phase: str = Form(""),
    cadence: str = Form(""),
    setting_sources: str = Form(""),
    system_prompt_override: str = Form(""),
    env_text: str = Form(""),
    claude_md: str = Form(""),
    prompt_py: str = Form(""),
):
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    config = registry.get_agent(agent_name)
    if not config:
        return HTMLResponse("Agent not found", status_code=404)

    _apply_form_to_config(
        config, description=description, model=model, max_turns=max_turns,
        max_budget_usd=max_budget_usd, max_concurrent=max_concurrent,
        permission_mode=permission_mode,
        tools=tools, thinking_type=thinking_type, status=status,
        phase=phase, cadence=cadence, setting_sources=setting_sources,
        system_prompt_override=system_prompt_override, env_text=env_text,
    )
    registry.save(settings.registry_path)
    _save_claude_md(config, claude_md, settings)

    # Save system prompt
    if prompt_py.strip():
        agent_dir_name = config.phase or config.name
        _write_agent_prompt(agent_dir_name, prompt_py)

    return RedirectResponse(url=f"/agents/{agent_name}", status_code=303)


@router.post("/{agent_name}/delete")
async def agent_delete(agent_name: str):
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    if agent_name in registry.agents:
        del registry.agents[agent_name]
        registry.save(settings.registry_path)
    return RedirectResponse(url="/agents/", status_code=303)


@router.get("/list")
async def api_list_agents():
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    return [vars(a) for a in registry.agents.values()]
