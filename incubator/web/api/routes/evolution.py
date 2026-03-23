"""Evolution routes: structured knowledge curation UI."""

from __future__ import annotations

import markdown as _markdown_lib
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from incubator.config import get_settings
from incubator.tools.knowledge_io import (
    delete_object,
    find_by_id,
    load_objects,
    save_object,
)
from incubator.web.api.paths import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_md = _markdown_lib.Markdown(extensions=["tables", "fenced_code", "nl2br", "toc"])


def _render_md(text: str) -> str:
    _md.reset()
    return _md.convert(text)


templates.env.filters["markdown"] = _render_md


@router.get("/", response_class=HTMLResponse)
async def evolution_view(request: Request):
    settings = get_settings()
    agents_dir = settings.project_root / "agents"

    agents_data = []
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
            continue
        knowledge_dir = agent_dir / "knowledge"
        objects = load_objects(knowledge_dir)
        if not objects:
            continue
        total_size = 0
        for o in objects:
            p = knowledge_dir / f"{o['id']}.yaml"
            if p.exists():
                total_size += p.stat().st_size
        no_justification = sum(1 for o in objects if not o.get("justification", "").strip())
        agents_data.append(
            {
                "name": agent_dir.name,
                "objects": sorted(objects, key=lambda o: o.get("confidence", 0), reverse=True),
                "count": len(objects),
                "size": total_size,
                "no_justification": no_justification,
            }
        )

    return templates.TemplateResponse(
        "evolution.html", {"request": request, "agents": agents_data}
    )


@router.get("/{agent}", response_class=HTMLResponse)
async def agent_knowledge_view(request: Request, agent: str):
    settings = get_settings()
    knowledge_dir = settings.project_root / "agents" / agent / "knowledge"
    objects = load_objects(knowledge_dir)
    objects.sort(key=lambda o: o.get("confidence", 0), reverse=True)
    return templates.TemplateResponse(
        "evolution.html",
        {
            "request": request,
            "agents": [
                {
                    "name": agent,
                    "objects": objects,
                    "count": len(objects),
                    "size": 0,
                    "no_justification": sum(1 for o in objects if not o.get("justification", "").strip()),
                }
            ]
            if objects
            else [],
            "single_agent": agent,
        },
    )


class EntryUpdate(BaseModel):
    insight: str = ""
    justification: str = ""
    confidence: float = 0.5


@router.put("/{agent}/{entry_id}", response_class=HTMLResponse)
async def update_entry(agent: str, entry_id: str, body: EntryUpdate):
    settings = get_settings()
    knowledge_dir = settings.project_root / "agents" / agent / "knowledge"
    obj = find_by_id(knowledge_dir, entry_id)
    if not obj:
        return HTMLResponse(f'<div class="text-[0.8rem] text-red-600">Entry {entry_id} not found</div>', status_code=404)

    if body.insight:
        obj["insight"] = body.insight
    if body.justification:
        obj["justification"] = body.justification
    obj["confidence"] = body.confidence
    from incubator.tools.knowledge_io import _now_iso
    obj["updated_at"] = _now_iso()
    save_object(knowledge_dir, obj)

    # Return updated card HTML fragment
    return HTMLResponse(_render_entry_card(agent, obj))


@router.delete("/{agent}/{entry_id}", response_class=HTMLResponse)
async def delete_entry(agent: str, entry_id: str):
    settings = get_settings()
    knowledge_dir = settings.project_root / "agents" / agent / "knowledge"
    if delete_object(knowledge_dir, entry_id):
        return HTMLResponse("")  # Empty response removes the element
    return HTMLResponse(f'<div class="text-[0.8rem] text-red-600">Entry {entry_id} not found</div>', status_code=404)


@router.post("/curate", response_class=JSONResponse)
async def trigger_curation(request: Request):
    """Trigger LLM curation (dry run) and return the proposed actions."""
    settings = get_settings()
    body = await request.json()
    agent_filter = body.get("agent")

    from incubator.orchestrator.evolution import EvolutionManager

    evo = EvolutionManager(settings.project_root, dispatcher=None)
    actions = await evo.run_retrospective(agent_filter=agent_filter, dry_run=True, no_llm=False)
    return JSONResponse({"actions": {k: v for k, v in actions.items()}})


@router.post("/curate/apply", response_class=JSONResponse)
async def apply_curation(request: Request):
    """Apply approved curation (no human gate — the UI click is the approval)."""
    settings = get_settings()
    body = await request.json()
    agent_filter = body.get("agent")

    from incubator.orchestrator.evolution import EvolutionManager

    evo = EvolutionManager(settings.project_root, dispatcher=None)
    # Run without dispatcher so it skips Telegram approval (UI is the gate)
    actions = await evo.run_retrospective(agent_filter=agent_filter, dry_run=False, no_llm=False)
    return JSONResponse({"applied": {k: len(v) for k, v in actions.items()}})


def _render_entry_card(agent: str, obj: dict) -> str:
    """Render a single entry card as HTML for HTMX partial updates."""
    obj_id = obj.get("id", "????")
    insight = _render_md(obj.get("insight", ""))
    justification = obj.get("justification", "").strip()
    confidence = obj.get("confidence", 0)
    predicates = obj.get("predicates", [])
    idea_context = obj.get("idea_context", [])
    updated_at = obj.get("updated_at", "")

    pred_pills = "".join(
        f'<span class="inline-block text-[0.65rem] font-mono bg-[#eee8f6] text-[#7b5ea8] px-2 py-0.5 rounded-full mr-1 mb-1">'
        f'{p[0]} &rarr; {p[1]} &rarr; {p[2]}</span>'
        for p in predicates
        if len(p) >= 3
    )

    ctx_pills = "".join(
        f'<span class="inline-block text-[0.65rem] font-mono bg-[#e3edf7] text-[#4a7aaa] px-2 py-0.5 rounded-full mr-1">{c}</span>'
        for c in idea_context
    )

    warning = ""
    if not justification:
        warning = '<span class="inline-block text-[0.65rem] bg-[#fce8e1] text-[#b85f3e] px-2 py-0.5 rounded-full font-medium">needs justification</span>'

    return f"""
    <div class="card p-4 mb-2" id="entry-{obj_id}">
        <div class="flex items-start justify-between gap-3 mb-2">
            <div class="flex items-center gap-2 flex-wrap">
                <span class="text-[0.68rem] font-mono text-[#b0a898]">[{obj_id}]</span>
                <span class="text-[0.68rem] font-mono text-[#8a8074]">conf: {confidence}</span>
                {warning}
            </div>
            <div class="flex gap-1 flex-shrink-0">
                <button onclick="deleteEntry('{agent}', '{obj_id}')" class="btn btn-danger text-[0.7rem] py-0.5 px-2">Delete</button>
            </div>
        </div>
        <div class="prose text-[0.84rem]">{insight}</div>
        {"<div class='mt-2 p-2 bg-[#f7f5f0] rounded-lg text-[0.8rem] text-[#544d43]'><span class='font-medium text-[#3b3530]'>Why this matters:</span> " + justification + "</div>" if justification else ""}
        <div class="mt-2 flex flex-wrap gap-1">
            {pred_pills}
        </div>
        {"<div class='mt-1 flex flex-wrap gap-1'>" + ctx_pills + "</div>" if ctx_pills else ""}
        <div class="mt-2 text-[0.65rem] text-[#b0a898]">Updated: {updated_at}</div>
    </div>
    """
