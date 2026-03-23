"""Idea CRUD + action routes."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from trellis.config import get_settings
from trellis.core.blackboard import Blackboard
from trellis.core.registry import load_registry
from trellis.web.api.filters import setup_filters
from trellis.web.api.paths import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
setup_filters(templates)


def _get_blackboard() -> Blackboard:
    return Blackboard(get_settings().blackboard_dir)


def _load_presets() -> dict:
    presets_path = get_settings().project_root / "pool" / "presets.json"
    if presets_path.exists():
        return json.loads(presets_path.read_text())
    return {}


def _get_registered_roles() -> set[str]:
    registry = load_registry(get_settings().registry_path)
    return {a.name for a in registry.agents.values()}


def _load_pool_running() -> set[tuple[str, str]]:
    """Read pool/state.json and return the set of (role, idea_id) currently running."""
    state_path = get_settings().project_root / "pool" / "state.json"
    if not state_path.exists():
        return set()
    try:
        state = json.loads(state_path.read_text())
        return {
            (w["role"], w.get("idea", ""))
            for w in state.get("workers", [])
            if w.get("status") == "active" and "role" in w
        }
    except (json.JSONDecodeError, OSError):
        return set()


def _compute_scheduling(bb: Blackboard, idea: dict, roles: list[str],
                        running: set[tuple[str, str]]) -> list[dict]:
    """Compute which roles an idea is eligible for.

    Returns a list of dicts: {role, reason, running}
    """
    idea_id = idea.get("id", idea.get("idea_id", ""))
    phase = idea.get("phase", "submitted")
    terminal = {"killed", "paused"}
    if phase in terminal or phase.endswith("_review"):
        return []
    if phase == "released" and not bb.pending_post_ready(idea_id):
        return []

    eligible = []
    is_ready = bb.is_ready(idea_id)

    if not is_ready:
        next_role = bb.next_agent(idea_id)
        if next_role and next_role in roles:
            eligible.append({
                "role": next_role,
                "reason": "next agent",
                "running": (next_role, idea_id) in running,
            })
    else:
        for post_role in bb.pending_post_ready(idea_id):
            if post_role in roles:
                eligible.append({
                    "role": post_role,
                    "reason": "post-ready",
                    "running": (post_role, idea_id) in running,
                })

    for role in roles:
        if any(e["role"] == role for e in eligible):
            continue
        if bb.has_pending_feedback(idea_id, role):
            eligible.append({
                "role": role,
                "reason": "feedback",
                "running": (role, idea_id) in running,
            })

    return eligible


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    bb = _get_blackboard()
    registry = load_registry(get_settings().registry_path)
    roles = [a.name for a in registry.agents.values() if a.status == "active"]
    pool_running = _load_pool_running()

    pipeline_stages = {"ideation", "implementation", "validation", "release"}
    auxiliary_roles = [r for r in roles if r not in pipeline_stages]

    ideas = []
    for idea_id in bb.list_ideas():
        status = bb.get_status(idea_id)
        status["idea_id"] = idea_id
        status["_scheduling"] = _compute_scheduling(bb, status, roles, pool_running)
        # Mark idea as running if any worker is active on it
        if any(idea_id == rid for _, rid in pool_running):
            status["running"] = True
        # Compute auxiliary agent status — idea's post_ready + global background agents
        aux_status = []
        idea_dir = bb.base_dir / idea_id
        pipeline = bb.get_pipeline(idea_id)
        post_ready_set = set(pipeline.get("post_ready", []))
        background_set = {
            a.name for a in registry.agents.values()
            if a.status == "active" and a.phase == "*"
        }
        idea_aux_roles = [r for r in auxiliary_roles if r in post_ready_set or r in background_set]
        for role in idea_aux_roles:
            role_file = idea_dir / f"{role}.md"
            role_dir = idea_dir / role
            has_run = role_file.exists() or (role_dir.exists() and any(role_dir.iterdir()))
            is_running = (role, idea_id) in pool_running
            aux_status.append({
                "role": role,
                "done": has_run and not is_running,
                "pending": is_running,
            })
        status["_auxiliary"] = aux_status
        ideas.append(status)
    ideas.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    return templates.TemplateResponse("home.html", {"request": request, "ideas": ideas})


@router.get("/ideas/new", response_class=HTMLResponse)
async def new_idea_form(request: Request):
    presets = _load_presets()
    return templates.TemplateResponse("new_idea.html", {"request": request, "presets": presets})


@router.get("/ideas")
async def ideas_redirect():
    return RedirectResponse(url="/", status_code=301)


@router.post("/ideas")
async def create_idea(
    title: str = Form(...),
    description: str = Form(...),
    preset: str = Form("full-pipeline"),
):
    bb = _get_blackboard()
    try:
        idea_id = bb.create_idea(title, description)
    except FileExistsError:
        # Idea with this slug already exists — redirect to it
        from trellis.core.blackboard import slugify
        return RedirectResponse(url=f"/ideas/{slugify(title)}", status_code=303)
    presets = _load_presets()
    preset_data = presets.get(preset, presets.get("full-pipeline", {}))
    if preset_data:
        pipeline = {
            "stages": preset_data.get("stages", []),
            "post_ready": preset_data.get("post_ready", []),
            "gating": preset_data.get("gating", {"default": "auto", "overrides": {}}),
            "preset": preset,
        }
        bb.set_pipeline(idea_id, pipeline)
    return RedirectResponse(url=f"/ideas/{idea_id}", status_code=303)


@router.get("/ideas/{idea_id}", response_class=HTMLResponse)
async def idea_detail(request: Request, idea_id: str):
    bb = _get_blackboard()
    status = bb.get_status(idea_id)

    # Organize files by category — idea.md first, then the rest
    artifacts = {}
    idea_dir = bb.idea_dir(idea_id)

    def _extract_title(content: str, filename: str) -> str:
        """Extract first H1 heading from markdown, fall back to filename."""
        if filename == "idea.md":
            return "Idea"
        m = re.match(r"^#\s+(.+)$", content.strip(), re.MULTILINE)
        if m:
            return m.group(1).strip()
        # Humanize filename: remove extension, replace hyphens
        return filename.rsplit(".", 1)[0].replace("-", " ").title()

    # Collect all files
    raw_artifacts = []
    for f in sorted(idea_dir.iterdir()):
        if f.is_dir():
            continue  # skip agent-logs/ directory
        if f.name in ("status.json", "feedback.json", "questions.json", "artifact-manifest.json"):
            continue
        content = f.read_text()
        is_empty = len(content.strip().split("\n")) <= 1
        raw_artifacts.append((f.name, {
            "content": content,
            "is_markdown": f.suffix == ".md",
            "is_empty": is_empty,
            "title": _extract_title(content, f.name),
        }))

    # Also pick up HTML artifacts from workspace
    workspace_dir = get_settings().project_root / "workspace" / idea_id
    if workspace_dir.is_dir():
        for f in sorted(workspace_dir.rglob("*.html")):
            rel = f.relative_to(workspace_dir)
            label = f"workspace/{rel}"
            raw_artifacts.append((label, {
                "content": f.read_text(),
                "is_markdown": False,
                "is_empty": False,
                "title": str(rel).rsplit(".", 1)[0].replace("-", " ").title(),
            }))

    # Sort: idea.md first, then alphabetically
    raw_artifacts.sort(key=lambda x: (0 if x[0] == "idea.md" else 1, x[0]))
    for name, info in raw_artifacts:
        artifacts[name] = info

    # Compute per-agent knowledge sizes (approx tokens = chars / 4)
    agent_knowledge = {}
    knowledge_dir = idea_dir / "agent-knowledge"
    if knowledge_dir.is_dir():
        for agent_dir in sorted(knowledge_dir.iterdir()):
            if agent_dir.is_dir():
                total_chars = 0
                file_count = 0
                for f in agent_dir.rglob("*"):
                    if f.is_file():
                        total_chars += f.stat().st_size
                        file_count += 1
                agent_knowledge[agent_dir.name] = {
                    "chars": total_chars,
                    "tokens": total_chars // 4,
                    "files": file_count,
                }

    # Count releases for refinement cycle display
    history = status.get("phase_history", [])
    release_count = sum(1 for entry in history if entry.get("to") == "released")

    is_running = status.get("running", False)
    stop_requested = status.get("stop_requested", False)

    pipeline = bb.get_pipeline(idea_id)
    registered_roles = _get_registered_roles()

    # Load feedback and questions
    feedback_entries = _load_feedback(bb, idea_id)
    question_entries = _load_questions(bb, idea_id)

    return templates.TemplateResponse(
        "idea_detail.html",
        {
            "request": request,
            "status": status,
            "artifacts": artifacts,
            "idea_id": idea_id,
            "agent_knowledge": agent_knowledge,
            "release_count": release_count,
            "is_running": is_running,
            "stop_requested": stop_requested,
            "pipeline": pipeline,
            "registered_roles": sorted(registered_roles),
            "feedback_entries": feedback_entries,
            "question_entries": question_entries,
        },
    )


@router.post("/ideas/{idea_id}/action")
async def idea_action(
    idea_id: str,
    action: str = Form(...),
    kill_reason: str = Form(""),
    resurrect_context: str = Form(""),
    refine_feedback: str = Form(""),
):
    settings = get_settings()
    bb = _get_blackboard()

    if action == "kill":
        from trellis.orchestrator.orchestrator import Orchestrator
        orch = Orchestrator(settings)
        await orch.kill(idea_id)
        if kill_reason.strip():
            bb.update_status(idea_id, kill_reason=kill_reason.strip())
    elif action == "resume":
        async def _run():
            from trellis.orchestrator.orchestrator import Orchestrator
            orch = Orchestrator(settings)
            await orch.resume(idea_id)

        asyncio.create_task(_run())
    elif action == "refine":
        if refine_feedback.strip():
            bb.append_file(
                idea_id, "idea.md",
                f"\n\n---\n\n## Refinement Feedback\n\n{refine_feedback.strip()}\n",
            )

        async def _run():
            from trellis.orchestrator.orchestrator import Orchestrator
            orch = Orchestrator(settings)
            from trellis.core.phase import Phase
            # Loop back to ideation — agents detect refinement mode automatically
            await orch._transition(idea_id, Phase.IDEATION)
            await orch.run_continuous_for_idea(idea_id)

        asyncio.create_task(_run())
    elif action == "dismiss_review":
        bb.update_status(idea_id, needs_human_review=False, review_reason=None)
    elif action == "delete":
        status = bb.get_status(idea_id)
        if status.get("phase") == "killed":
            bb.delete_idea(idea_id)
            return RedirectResponse(url="/", status_code=303)
    elif action == "resurrect":
        from trellis.core.phase import Phase
        bb.set_phase(idea_id, Phase.SUBMITTED)
        bb.update_status(idea_id, kill_reason=None)
        if resurrect_context.strip():
            # Append context to idea.md so agents see it on next run
            bb.append_file(
                idea_id, "idea.md",
                f"\n\n---\n\n## Additional Context (Resurrected)\n\n{resurrect_context.strip()}\n",
            )

    return RedirectResponse(url=f"/ideas/{idea_id}", status_code=303)


@router.post("/ideas/{idea_id}/pipeline")
async def update_pipeline(
    idea_id: str,
    stages: str = Form(""),
    post_ready: str = Form(""),
    gating_default: str = Form("auto"),
    gating_overrides: str = Form("{}"),
):
    bb = _get_blackboard()
    stage_list = [s.strip() for s in stages.split(",") if s.strip()]
    post_ready_list = [s.strip() for s in post_ready.split(",") if s.strip()]

    # Validate stage names against registry and known pipeline stage names
    registered = _get_registered_roles()
    # Also accept short names used in presets (e.g. "competitive" vs "competitive-watcher")
    pipeline_known = {"ideation", "implementation", "validation", "release", "competitive", "research"}
    allowed = registered | pipeline_known
    stage_list = [s for s in stage_list if s in allowed]
    post_ready_list = [s for s in post_ready_list if s in allowed]

    try:
        overrides = json.loads(gating_overrides) if gating_overrides.strip() else {}
    except json.JSONDecodeError:
        overrides = {}

    pipeline = {
        "agents": stage_list,
        "post_ready": post_ready_list,
        "parallel_groups": [stage_list] + ([post_ready_list] if post_ready_list else []),
        "gating": {"default": gating_default, "overrides": overrides},
    }
    bb.set_pipeline(idea_id, pipeline)
    return RedirectResponse(url=f"/ideas/{idea_id}", status_code=303)


@router.get("/ideas/{idea_id}/logs", response_class=HTMLResponse)
async def idea_agent_logs(request: Request, idea_id: str):
    bb = _get_blackboard()
    status = bb.get_status(idea_id)
    log_dir = bb.idea_dir(idea_id) / "agent-logs"
    logs = []
    if log_dir.is_dir():
        for f in sorted(log_dir.iterdir(), reverse=True):
            if f.suffix == ".json":
                data = json.loads(f.read_text())
                logs.append({
                    "filename": f.name,
                    "agent": data.get("agent", "unknown"),
                    "timestamp": data.get("timestamp", ""),
                    "model": data.get("model", ""),
                    "transcript_len": len(data.get("transcript", [])),
                })
    return templates.TemplateResponse(
        "idea_logs.html",
        {"request": request, "status": status, "logs": logs, "idea_id": idea_id},
    )


@router.get("/ideas/{idea_id}/logs/{log_filename}", response_class=HTMLResponse)
async def idea_agent_log_detail(request: Request, idea_id: str, log_filename: str):
    bb = _get_blackboard()
    status = bb.get_status(idea_id)
    log_file = bb.idea_dir(idea_id) / "agent-logs" / log_filename
    if not log_file.exists():
        return HTMLResponse("Log not found", status_code=404)
    log_data = json.loads(log_file.read_text())
    return templates.TemplateResponse(
        "idea_log_detail.html",
        {"request": request, "status": status, "log": log_data, "idea_id": idea_id, "log_filename": log_filename},
    )


@router.get("/api/ideas")
async def api_list_ideas():
    bb = _get_blackboard()
    return [bb.get_status(idea_id) for idea_id in bb.list_ideas()]


@router.get("/api/ideas/{idea_id}")
async def api_get_idea(idea_id: str):
    bb = _get_blackboard()
    return bb.get_status(idea_id)


# ── Feedback ────────────────────────────────────────────────────────


def _load_feedback(bb: Blackboard, idea_id: str) -> list[dict]:
    try:
        raw = bb.read_file(idea_id, "feedback.json")
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_feedback(bb: Blackboard, idea_id: str, entries: list[dict]) -> None:
    bb.write_file(idea_id, "feedback.json", json.dumps(entries, indent=2))


@router.post("/api/ideas/{idea_id}/feedback")
async def submit_feedback(
    idea_id: str,
    artifact: str = Form(""),
    selected_text: str = Form(""),
    comment: str = Form(""),
):
    if not comment.strip():
        return JSONResponse({"error": "Comment is required"}, status_code=400)

    bb = _get_blackboard()
    entries = _load_feedback(bb, idea_id)

    # Determine which agents should process this feedback:
    # all roles that have previously serviced this idea.
    status = bb.get_status(idea_id)
    pending_agents = list(status.get("last_serviced_by", {}).keys())

    entry = {
        "id": str(uuid.uuid4())[:8],
        "artifact": artifact,
        "selected_text": selected_text.strip(),
        "comment": comment.strip(),
        "from_identity": "v1:user:me",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pending_agents": pending_agents,
        "acknowledged_by": [],
    }
    entries.append(entry)
    _save_feedback(bb, idea_id, entries)
    return JSONResponse({"ok": True, "entry": entry})


@router.get("/api/ideas/{idea_id}/feedback")
async def list_feedback(idea_id: str):
    bb = _get_blackboard()
    return _load_feedback(bb, idea_id)


# ── Questions ───────────────────────────────────────────────────────


def _load_questions(bb: Blackboard, idea_id: str) -> list[dict]:
    try:
        raw = bb.read_file(idea_id, "questions.json")
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_questions(bb: Blackboard, idea_id: str, entries: list[dict]) -> None:
    bb.write_file(idea_id, "questions.json", json.dumps(entries, indent=2))


def _build_artifact_context(bb: Blackboard, idea_id: str) -> str:
    """Build a context string from all blackboard artifacts for the question agent."""
    idea_dir = bb.idea_dir(idea_id)
    parts = []
    for f in sorted(idea_dir.iterdir()):
        if f.is_dir() or f.name in ("status.json", "feedback.json", "questions.json"):
            continue
        content = f.read_text()
        if not content.strip():
            continue
        # Truncate very large files
        if len(content) > 20_000:
            content = content[:20_000] + "\n\n[... truncated]"
        parts.append(f"### {f.name}\n{content}")
    return "\n\n---\n\n".join(parts)


async def _generate_multi_perspective_answer(
    question: str, artifact_context: str, feedback_context: str,
) -> dict:
    """Call Claude Agent SDK to generate a multi-perspective answer."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

    system = (
        "You are a panel of expert advisors analyzing an incubated idea. "
        "You have access to all artifacts produced by the agents that worked on this idea. "
        "Answer the user's question from four distinct expert perspectives, then provide "
        "an integrated synthesis.\n\n"
        "Format your response EXACTLY as follows (use these exact headings):\n\n"
        "## Research & Market\n[Analysis from the market research perspective]\n\n"
        "## Technical\n[Analysis from the engineering/architecture perspective]\n\n"
        "## Quality & Risk\n[Analysis from the QA/validation perspective]\n\n"
        "## Launch & Strategy\n[Analysis from the go-to-market perspective]\n\n"
        "## Synthesis\n[Integrated answer that weighs all perspectives and gives a clear, "
        "actionable conclusion]\n\n"
        "Be concise and specific. Reference actual data from the artifacts. "
        "Do not use emoji."
    )

    user_msg = f"## Artifacts\n\n{artifact_context}"
    if feedback_context:
        user_msg += f"\n\n## Human Feedback\n\n{feedback_context}"
    user_msg += f"\n\n## Question\n\n{question}"

    answer_text = ""
    async for message in query(
        prompt=user_msg,
        options=ClaudeAgentOptions(
            system_prompt=system,
            model="claude-sonnet-4-6",
            max_turns=1,
            allowed_tools=[],
        ),
    ):
        if isinstance(message, ResultMessage):
            answer_text = message.result or ""

    return {
        "answer": answer_text,
        "model": "claude-sonnet-4-6",
    }


@router.post("/api/ideas/{idea_id}/question")
async def submit_question(idea_id: str, question: str = Form("")):
    if not question.strip():
        return JSONResponse({"error": "Question is required"}, status_code=400)

    bb = _get_blackboard()
    artifact_context = _build_artifact_context(bb, idea_id)

    # Include any existing feedback as context
    feedback_entries = _load_feedback(bb, idea_id)
    feedback_context = ""
    if feedback_entries:
        feedback_parts = []
        for fb in feedback_entries:
            part = f"- On **{fb['artifact']}**"
            if fb.get("selected_text"):
                part += f' (re: "{fb["selected_text"][:100]}")'
            part += f": {fb['comment']}"
            feedback_parts.append(part)
        feedback_context = "\n".join(feedback_parts)

    try:
        result = await _generate_multi_perspective_answer(
            question.strip(), artifact_context, feedback_context,
        )
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to generate answer: {e}"},
            status_code=500,
        )

    entries = _load_questions(bb, idea_id)
    entry = {
        "id": str(uuid.uuid4())[:8],
        "question": question.strip(),
        "answer": result["answer"],
        "model": result["model"],
        "tokens": result.get("input_tokens", 0) + result.get("output_tokens", 0),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    entries.append(entry)
    _save_questions(bb, idea_id, entries)

    return JSONResponse({"ok": True, "entry": entry})


@router.get("/api/ideas/{idea_id}/questions")
async def list_questions(idea_id: str):
    bb = _get_blackboard()
    return _load_questions(bb, idea_id)
