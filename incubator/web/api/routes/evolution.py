"""Evolution history and trigger routes."""

from __future__ import annotations

import markdown as _markdown_lib
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from incubator.config import get_settings

router = APIRouter()
templates = Jinja2Templates(
    directory=str(get_settings().project_root / "incubator" / "web" / "frontend" / "templates")
)

_md = _markdown_lib.Markdown(extensions=["tables", "fenced_code", "nl2br", "toc"])


def _render_md(text: str) -> str:
    _md.reset()
    return _md.convert(text)


templates.env.filters["markdown"] = _render_md


@router.get("/", response_class=HTMLResponse)
async def evolution_view(request: Request):
    settings = get_settings()
    agents_dir = settings.project_root / "incubator" / "agents"

    learnings = []
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
            continue
        knowledge_path = agent_dir / "knowledge" / "learnings.md"
        if knowledge_path.exists():
            learnings.append(
                {
                    "agent": agent_dir.name,
                    "content": knowledge_path.read_text(),
                    "size": knowledge_path.stat().st_size,
                }
            )

    return templates.TemplateResponse(
        "evolution.html", {"request": request, "learnings": learnings}
    )
