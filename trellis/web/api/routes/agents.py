"""Agent status and management routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from trellis.config import get_settings
from trellis.core.blackboard import Blackboard
from trellis.core.registry import AgentConfig, load_registry
from trellis.web.api.filters import setup_filters
from trellis.web.api.paths import TEMPLATES_DIR
from trellis.web.api.routes.settings import _read_agent_prompt, _write_agent_prompt

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
setup_filters(templates)

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
    from trellis.tools.knowledge_io import load_objects
    objects = load_objects(knowledge_dir)
    data["knowledge_size"] = sum(
        (knowledge_dir / f"{o['id']}.yaml").stat().st_size
        for o in objects
        if (knowledge_dir / f"{o['id']}.yaml").exists()
    ) if objects else 0
    data["knowledge_count"] = len(objects)

    claude_md = ""
    if agent.claude_home:
        claude_md_path = settings.project_root / agent.claude_home / "CLAUDE.md"
        if claude_md_path.exists():
            claude_md = claude_md_path.read_text()
    data["claude_md"] = claude_md

    # Load prompt.py content — check agent's own dir first, then phase dir
    prompt_py = _read_agent_prompt(agent.name)
    if prompt_py is None and agent.phase and agent.phase not in ("*", ""):
        prompt_py = _read_agent_prompt(agent.phase)
    data["prompt_py"] = prompt_py or ""
    prompt_path = (
        settings.project_root / "trellis" / "agents"
        / (agent.phase or agent.name) / "prompt.py"
    )
    data["prompt_path"] = str(prompt_path.relative_to(settings.project_root)) if prompt_path.exists() else ""
    return data


def _apply_form_to_config(config: AgentConfig, *, description: str, model: str,
                           max_turns: int, max_budget_usd: float, max_concurrent: int,
                           permission_mode: str,
                           tools: str, thinking_type: str, status: str,
                           phase: str, cadence: str, setting_sources: str,
                           system_prompt_override: str, env_text: str,
                           # Sandbox fields (all optional with safe defaults)
                           sandbox_enabled: str = "",
                           sandbox_ssh: str = "",
                           sandbox_rollback: str = "",
                           sandbox_verify_attestations: str = "",
                           sandbox_proxy_credentials: str = "",
                           sandbox_allowed_hosts: str = "",
                           sandbox_allowed_ports: str = "",
                           sandbox_allowed_commands: str = "",
                           sandbox_extra_read_paths: str = "",
                           sandbox_extra_write_paths: str = "",
                           sandbox_credential_maps: str = "",
                           sandbox_profile: str = "claude-code") -> None:
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

    # Sandbox fields
    config.sandbox_enabled = sandbox_enabled == "true"
    config.sandbox_ssh = sandbox_ssh == "true"
    config.sandbox_rollback = sandbox_rollback == "true"
    config.sandbox_verify_attestations = sandbox_verify_attestations == "true"
    config.sandbox_profile = sandbox_profile or "claude-code"
    config.sandbox_proxy_credentials = [c.strip() for c in sandbox_proxy_credentials.split(",") if c.strip()] or ["anthropic"]
    config.sandbox_allowed_hosts = [h.strip() for h in sandbox_allowed_hosts.split(",") if h.strip()]
    config.sandbox_allowed_ports = [int(p.strip()) for p in sandbox_allowed_ports.split(",") if p.strip().isdigit()]
    config.sandbox_allowed_commands = [c.strip() for c in sandbox_allowed_commands.split(",") if c.strip()]
    config.sandbox_extra_read_paths = [p.strip() for p in sandbox_extra_read_paths.split(",") if p.strip()]
    config.sandbox_extra_write_paths = [p.strip() for p in sandbox_extra_write_paths.split(",") if p.strip()]
    config.sandbox_credential_maps = [m.strip() for m in sandbox_credential_maps.splitlines() if m.strip()]


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


@router.get("/wizard", response_class=HTMLResponse)
async def agent_wizard(request: Request):
    ctx = {"request": request, "all_tools": ALL_TOOLS}
    return templates.TemplateResponse("agent_wizard.html", ctx)


class GenerateRequest(BaseModel):
    description: str
    agent_type: str = ""  # pipeline, watcher, global, or empty for auto-detect


@router.post("/wizard/generate")
async def wizard_generate(req: GenerateRequest):
    """Use Claude to generate a full agent config from a natural language description."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

    system = """\
You are an agent architect for an idea trellis system. Given a description of what \
an agent should do, generate a complete agent configuration as JSON.

Trellis has these agent types:
- **pipeline**: Runs as part of the idea pipeline during a specific phase (ideation, \
implementation, validation, release). Has read/write access to the blackboard.
- **watcher**: Runs on a cron schedule to monitor ideas. Has read-only blackboard \
access plus register_feedback. Cannot write artifacts.
- **global**: Runs against all ideas (phase="*"). Used for cross-cutting analysis.

Available tools: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Agent, AskUserQuestion

Respond with ONLY a JSON object (no markdown fences, no explanation) with these fields:
{
  "name": "slug-name",
  "description": "1-2 sentence description",
  "type": "pipeline|watcher|global",
  "phase": "ideation|implementation|validation|release|*|null",
  "cadence": "cron expression or null",
  "model": "claude-sonnet-4-6|claude-opus-4-6|claude-haiku-4-5",
  "tools": ["Read", ...],
  "system_prompt": "The full system prompt for this agent. Be thorough and specific.",
  "knowledge_suggestions": ["Short description of knowledge this agent needs"],
  "claude_md": "Optional CLAUDE.md content with behavioral rules",
  "sandbox_enabled": true,
  "sandbox_ssh": false,
  "sandbox_proxy_credentials": ["anthropic"],
  "sandbox_allowed_hosts": []
}

Guidelines:
- Watcher agents NEVER get Write tool. They use register_feedback only.
- Pipeline agents that need web research get WebSearch and WebFetch.
- Use haiku for simple/cheap tasks, sonnet for balanced, opus for complex reasoning.
- System prompts should be detailed and specific to the agent's role.
- Name should be a short kebab-case slug.
- Knowledge suggestions should be 2-4 practical topics the agent would benefit from.
- Set sandbox_ssh=true for implementation/release agents that need git operations.
- Set sandbox_allowed_hosts to any external APIs the agent needs to reach (e.g. ["api.github.com"] for release agents).
- Ideation agents should NOT have Bash or Agent tools (use WebSearch/WebFetch instead)."""

    user_msg = f"Create an agent that: {req.description}"
    if req.agent_type:
        user_msg += f"\n\nThe user wants this to be a {req.agent_type} agent."

    result_text = ""
    async for message in query(
        prompt=user_msg,
        options=ClaudeAgentOptions(
            system_prompt=system,
            model="claude-haiku-4-5",
            max_turns=1,
            allowed_tools=[],
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""

    # Parse the JSON from the response
    import json as _json
    try:
        # Strip any markdown fences if present
        text = result_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        config = _json.loads(text)
    except (_json.JSONDecodeError, IndexError):
        return JSONResponse({"error": "Failed to parse LLM response", "raw": result_text}, status_code=500)

    return JSONResponse(config)


@router.get("/plugins", response_class=HTMLResponse)
async def plugins_marketplace(request: Request):
    return templates.TemplateResponse("plugins.html", {"request": request})


@router.get("/plugins/api")
async def plugins_api():
    """Read real marketplace data from the Claude Code plugin cache."""
    import json as _json
    from pathlib import Path

    claude_dir = Path.home() / ".claude" / "plugins"
    result_marketplaces = []
    result_plugins = []

    # Read known_marketplaces.json
    known_path = claude_dir / "known_marketplaces.json"
    known = {}
    if known_path.exists():
        try:
            known = _json.loads(known_path.read_text())
        except Exception:
            pass

    # Read each marketplace's marketplace.json
    mp_dir = claude_dir / "marketplaces"
    if mp_dir.exists():
        for mp_name, mp_info in known.items():
            install_loc = mp_info.get("installLocation", str(mp_dir / mp_name))
            mj_path = Path(install_loc) / ".claude-plugin" / "marketplace.json"
            if not mj_path.exists():
                result_marketplaces.append({
                    "name": mp_name,
                    "source": mp_info.get("source", {}).get("repo", ""),
                    "plugin_count": 0,
                    "description": "",
                })
                continue

            try:
                mj = _json.loads(mj_path.read_text())
            except Exception:
                continue

            plugins_list = mj.get("plugins", [])
            result_marketplaces.append({
                "name": mj.get("name", mp_name),
                "source": mp_info.get("source", {}).get("repo", str(mp_info.get("source", ""))),
                "plugin_count": len(plugins_list),
                "description": (mj.get("metadata", {}) or {}).get("description", ""),
            })

            for p in plugins_list:
                result_plugins.append({
                    "name": p.get("name", ""),
                    "description": p.get("description", ""),
                    "category": p.get("category", ""),
                    "version": p.get("version", ""),
                    "marketplace": mj.get("name", mp_name),
                    "homepage": p.get("homepage", ""),
                    "author": (p.get("author", {}) or {}).get("name", ""),
                })

    # Also check settings for extraKnownMarketplaces that aren't installed yet
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text())
            for name, conf in settings.get("extraKnownMarketplaces", {}).items():
                if name not in known:
                    source = conf.get("source", {})
                    result_marketplaces.append({
                        "name": name,
                        "source": source.get("repo", str(source)),
                        "plugin_count": 0,
                        "description": "(not yet synced)",
                    })
        except Exception:
            pass

    return JSONResponse({
        "marketplaces": result_marketplaces,
        "plugins": result_plugins,
    })


class AddMarketplaceRequest(BaseModel):
    source: str


@router.post("/plugins/api/add-marketplace")
async def add_marketplace_api(req: AddMarketplaceRequest):
    """Add a marketplace source to settings.json extraKnownMarketplaces."""
    import json as _json
    from pathlib import Path

    source = req.source.strip()
    if not source:
        return JSONResponse({"error": "Source is required"}, status_code=400)

    settings_path = Path.home() / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text())
        except Exception:
            pass

    extra = settings.setdefault("extraKnownMarketplaces", {})

    # Determine source type and name
    if "/" in source and not source.startswith(("http", "git@", ".", "/")):
        # GitHub shorthand: owner/repo
        name = source.replace("/", "-")
        extra[name] = {"source": {"source": "github", "repo": source}}
    elif source.startswith(("http://", "https://", "git@")):
        name = source.split("/")[-1].replace(".git", "")
        extra[name] = {"source": {"source": "url", "url": source}}
    else:
        return JSONResponse({"error": "Unsupported source format. Use owner/repo or a git URL."}, status_code=400)

    settings_path.write_text(_json.dumps(settings, indent=2))

    return JSONResponse({
        "ok": True,
        "name": name,
        "message": f"Added marketplace '{name}'. Run '/plugin marketplace add {source}' in Claude Code to sync plugins.",
    })


@router.get("/new", response_class=HTMLResponse)
async def new_agent_form(request: Request):
    blank = {
        "name": "", "description": "", "model": "claude-sonnet-4-6",
        "max_turns": 50, "max_budget_usd": 1.0, "status": "active",
        "tools": [], "phase": "", "cadence": "", "permission_mode": "bypassPermissions",
        "thinking": {"type": "adaptive"}, "setting_sources": ["project"],
        "env": None, "system_prompt_override": "", "claude_home": "",
        "claude_md": "", "knowledge_size": 0,
        # Sandbox defaults
        "sandbox_enabled": False, "sandbox_ssh": False, "sandbox_rollback": False,
        "sandbox_verify_attestations": False, "sandbox_profile": "claude-code",
        "sandbox_proxy_credentials": ["anthropic"], "sandbox_allowed_hosts": [],
        "sandbox_allowed_ports": [], "sandbox_allowed_commands": [],
        "sandbox_extra_read_paths": [], "sandbox_extra_write_paths": [],
        "sandbox_credential_maps": [],
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
    sandbox_enabled: str = Form(""),
    sandbox_ssh: str = Form(""),
    sandbox_rollback: str = Form(""),
    sandbox_verify_attestations: str = Form(""),
    sandbox_proxy_credentials: str = Form("anthropic"),
    sandbox_allowed_hosts: str = Form(""),
    sandbox_allowed_ports: str = Form(""),
    sandbox_allowed_commands: str = Form(""),
    sandbox_extra_read_paths: str = Form(""),
    sandbox_extra_write_paths: str = Form(""),
    sandbox_credential_maps: str = Form(""),
    sandbox_profile: str = Form("claude-code"),
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
        sandbox_enabled=sandbox_enabled, sandbox_ssh=sandbox_ssh,
        sandbox_rollback=sandbox_rollback, sandbox_verify_attestations=sandbox_verify_attestations,
        sandbox_proxy_credentials=sandbox_proxy_credentials,
        sandbox_allowed_hosts=sandbox_allowed_hosts, sandbox_allowed_ports=sandbox_allowed_ports,
        sandbox_allowed_commands=sandbox_allowed_commands,
        sandbox_extra_read_paths=sandbox_extra_read_paths,
        sandbox_extra_write_paths=sandbox_extra_write_paths,
        sandbox_credential_maps=sandbox_credential_maps, sandbox_profile=sandbox_profile,
    )

    registry.register_agent(config, settings.registry_path)
    _save_claude_md(config, claude_md, settings)

    return RedirectResponse(url=f"/agents/{slug}", status_code=303)


@router.get("/list")
async def api_list_agents():
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    return [vars(a) for a in registry.agents.values()]


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
    sandbox_enabled: str = Form(""),
    sandbox_ssh: str = Form(""),
    sandbox_rollback: str = Form(""),
    sandbox_verify_attestations: str = Form(""),
    sandbox_proxy_credentials: str = Form("anthropic"),
    sandbox_allowed_hosts: str = Form(""),
    sandbox_allowed_ports: str = Form(""),
    sandbox_allowed_commands: str = Form(""),
    sandbox_extra_read_paths: str = Form(""),
    sandbox_extra_write_paths: str = Form(""),
    sandbox_credential_maps: str = Form(""),
    sandbox_profile: str = Form("claude-code"),
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
        sandbox_enabled=sandbox_enabled, sandbox_ssh=sandbox_ssh,
        sandbox_rollback=sandbox_rollback, sandbox_verify_attestations=sandbox_verify_attestations,
        sandbox_proxy_credentials=sandbox_proxy_credentials,
        sandbox_allowed_hosts=sandbox_allowed_hosts, sandbox_allowed_ports=sandbox_allowed_ports,
        sandbox_allowed_commands=sandbox_allowed_commands,
        sandbox_extra_read_paths=sandbox_extra_read_paths,
        sandbox_extra_write_paths=sandbox_extra_write_paths,
        sandbox_credential_maps=sandbox_credential_maps, sandbox_profile=sandbox_profile,
    )
    registry.save(settings.registry_path)
    _save_claude_md(config, claude_md, settings)

    # Save system prompt — always to the agent's own directory
    if prompt_py.strip():
        _write_agent_prompt(config.name, prompt_py)

    return RedirectResponse(url=f"/agents/{agent_name}", status_code=303)


@router.post("/{agent_name}/delete")
async def agent_delete(agent_name: str):
    settings = get_settings()
    registry = load_registry(settings.registry_path)
    if agent_name in registry.agents:
        del registry.agents[agent_name]
        registry.save(settings.registry_path)
    return RedirectResponse(url="/agents/", status_code=303)
