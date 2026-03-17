"""MCP tools for blackboard read/write operations.

These are registered as custom tools on an MCP server that gets
passed to agents via the Claude Agent SDK's `mcp_servers` option.
"""

from __future__ import annotations

from claude_agent_sdk import tool, create_sdk_mcp_server

from incubator.core.blackboard import Blackboard

ARTIFACT_REJECTION_MSG = """REJECTED: '{filename}' is a markdown file. All artifacts MUST be \
self-contained .html files.

Write '{html_name}' instead — a single HTML file with:
- Inline CSS with modern design (gradients, backdrop-blur, subtle animations)
- Inline SVG data visualizations (charts, diagrams, progress indicators)
- A sophisticated color palette that fits the idea's personality
- Grid/flexbox layouts, cards, visual hierarchy
- Professional consulting-quality presentation
- NO external dependencies (no CDN links, no external CSS/JS)

Choose a descriptive filename that reflects the content (e.g., market-landscape.html, \
risk-analysis.html, architecture-overview.html). You decide which artifacts to create."""


def create_blackboard_mcp_server(blackboard: Blackboard, idea_id: str, agent_role: str = ""):
    """Create an MCP server with blackboard tools scoped to a specific idea."""

    @tool(
        "read_blackboard",
        "Read a file from the idea's blackboard directory",
        {"filename": str},
    )
    async def read_blackboard(args):
        filename = args["filename"]
        try:
            content = blackboard.read_file(idea_id, filename)
            return {"content": [{"type": "text", "text": content}]}
        except FileNotFoundError:
            return {
                "content": [{"type": "text", "text": f"File not found: {filename}"}],
                "isError": True,
            }

    @tool(
        "write_blackboard",
        (
            "Write content to a file on the idea's blackboard. "
            "All artifacts MUST be .html files — gorgeous, self-contained HTML with inline "
            "CSS/JS and SVG visualizations. You choose which artifacts to create based on "
            "what's most valuable for this specific idea."
        ),
        {"filename": str, "content": str},
    )
    async def write_blackboard(args):
        filename = args["filename"]

        # Agents can only write .html artifacts — system files are read-only
        if filename.endswith(".md") or filename == "status.json":
            html_name = filename.rsplit(".", 1)[0] + ".html"
            return {
                "content": [{
                    "type": "text",
                    "text": ARTIFACT_REJECTION_MSG.format(
                        filename=filename, html_name=html_name
                    ),
                }],
                "isError": True,
            }

        blackboard.write_file(idea_id, filename, args["content"])
        return {"content": [{"type": "text", "text": f"Written: {filename}"}]}

    @tool(
        "append_blackboard",
        "Append content to a file on the idea's blackboard",
        {"filename": str, "content": str},
    )
    async def append_blackboard(args):
        filename = args["filename"]

        if filename.endswith(".md") or filename == "status.json":
            html_name = filename.rsplit(".", 1)[0] + ".html"
            return {
                "content": [{
                    "type": "text",
                    "text": ARTIFACT_REJECTION_MSG.format(
                        filename=filename, html_name=html_name
                    ),
                }],
                "isError": True,
            }

        blackboard.append_file(idea_id, filename, args["content"])
        return {"content": [{"type": "text", "text": f"Appended to: {filename}"}]}

    @tool(
        "get_idea_status",
        "Get the current status of this idea including phase and metadata",
        {},
    )
    async def get_idea_status(args):
        import json

        status = blackboard.get_status(idea_id)
        return {"content": [{"type": "text", "text": json.dumps(status, indent=2)}]}

    @tool(
        "set_phase_recommendation",
        "Set a recommendation for the next phase transition",
        {"recommendation": str, "reasoning": str},
    )
    async def set_phase_recommendation(args):
        blackboard.update_status(
            idea_id,
            phase_recommendation=args["recommendation"],
            phase_reasoning=args.get("reasoning", ""),
        )
        return {
            "content": [
                {"type": "text", "text": f"Recommendation set: {args['recommendation']}"}
            ]
        }

    @tool(
        "list_blackboard_files",
        "List all files in this idea's blackboard directory",
        {},
    )
    async def list_blackboard_files(args):
        idea_dir = blackboard.idea_dir(idea_id)
        files = [f.name for f in idea_dir.iterdir() if f.is_file()]
        return {"content": [{"type": "text", "text": "\n".join(sorted(files))}]}

    @tool(
        "declare_artifacts",
        (
            "Register the artifacts you created and why. Call this BEFORE "
            "set_phase_recommendation. This creates a structured manifest that "
            "helps downstream agents understand your outputs."
        ),
        {"artifacts": list},
    )
    async def declare_artifacts(args):
        """Each artifact should be: {"file": "name.html", "purpose": "...", "confidence": 0.0-1.0}"""
        import json as _json

        artifacts = args.get("artifacts", [])
        if not artifacts:
            return {
                "content": [{"type": "text", "text": "No artifacts declared."}],
                "isError": True,
            }

        # Validate each artifact entry
        manifest = []
        for art in artifacts:
            if not isinstance(art, dict) or "file" not in art:
                continue
            entry = {
                "file": art["file"],
                "purpose": art.get("purpose", ""),
                "confidence": art.get("confidence", 1.0),
            }
            # Verify the file actually exists on the blackboard
            if not blackboard.file_exists(idea_id, entry["file"]):
                entry["warning"] = "file not found on blackboard"
            manifest.append(entry)

        # Persist the manifest to the blackboard
        manifest_data = {
            "declared_by": agent_role or blackboard.get_status(idea_id).get("phase", "unknown"),
            "artifacts": manifest,
        }

        # Read existing manifest and append (multiple agents contribute)
        manifest_file = "artifact-manifest.json"
        existing = []
        try:
            raw = blackboard.read_file(idea_id, manifest_file)
            existing = _json.loads(raw)
            if not isinstance(existing, list):
                existing = [existing]
        except (FileNotFoundError, _json.JSONDecodeError):
            existing = []

        existing.append(manifest_data)
        blackboard.write_file(idea_id, manifest_file, _json.dumps(existing, indent=2))

        summary = "\n".join(
            f"  - {a['file']}: {a['purpose']}" + (f" (confidence: {a['confidence']})" if a.get('confidence', 1.0) < 1.0 else "")
            for a in manifest
        )
        return {
            "content": [{"type": "text", "text": f"Declared {len(manifest)} artifact(s):\n{summary}"}]
        }

    @tool(
        "register_feedback",
        (
            "Register feedback on an artifact. Use this to report issues found "
            "during review. Do NOT use write_blackboard for feedback."
        ),
        {
            "artifact": str,
            "comment": str,
            "severity": str,
            "pending_agents": list,
        },
    )
    async def register_feedback(args):
        import json as _json
        from datetime import datetime, timezone
        import uuid as _uuid

        artifact = args.get("artifact", "")
        comment = args.get("comment", "")
        severity = args.get("severity", "structure")
        caller_pending = args.get("pending_agents", None)

        if not comment:
            return {
                "content": [{"type": "text", "text": "Comment is required"}],
                "isError": True,
            }

        identity = f"v1:agent:{agent_role}" if agent_role else "v1:agent:unknown"

        # Load existing feedback
        try:
            raw = blackboard.read_file(idea_id, "feedback.json")
            entries = _json.loads(raw)
            if not isinstance(entries, list):
                entries = []
        except (FileNotFoundError, _json.JSONDecodeError):
            entries = []

        # Determine pending_agents: caller-specified, artifact-owner lookup, or fallback
        # Coerce string to list (agents sometimes pass "role1,role2" instead of a list)
        if isinstance(caller_pending, str):
            caller_pending = [x.strip() for x in caller_pending.split(",") if x.strip()]
        if caller_pending:
            pending_agents = caller_pending
        elif artifact:
            # Look up who owns this artifact from the manifest
            owners = _find_artifact_owners(blackboard, idea_id, artifact)
            if owners:
                pending_agents = owners
            else:
                # Fallback: all previously-serviced roles
                status = blackboard.get_status(idea_id)
                pending_agents = list(status.get("last_serviced_by", {}).keys())
        else:
            status = blackboard.get_status(idea_id)
            pending_agents = list(status.get("last_serviced_by", {}).keys())

        entry = {
            "id": str(_uuid.uuid4())[:8],
            "artifact": artifact,
            "comment": comment,
            "severity": severity,
            "from_identity": identity,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pending_agents": pending_agents,
            "acknowledged_by": [],
        }
        entries.append(entry)
        blackboard.write_file(idea_id, "feedback.json", _json.dumps(entries, indent=2))

        return {
            "content": [{"type": "text", "text": f"Feedback registered: [{severity}] {artifact} — {comment[:80]}"}]
        }

    @tool(
        "acknowledge_feedback",
        (
            "Acknowledge that you have reviewed a feedback entry. Call this for EACH "
            "feedback item after you have considered it. You do not need to modify "
            "any artifact — just indicate you reviewed the feedback. If the feedback "
            "is outside your area of expertise, acknowledge it anyway with a note."
        ),
        {"feedback_id": str, "action_taken": str},
    )
    async def acknowledge_feedback(args):
        feedback_id = args["feedback_id"]
        action_taken = args.get("action_taken", "reviewed")
        role = agent_role

        if not role:
            return {
                "content": [{"type": "text", "text": "Cannot acknowledge: agent role not set"}],
                "isError": True,
            }

        found = blackboard.acknowledge_feedback(idea_id, feedback_id, role)
        if not found:
            return {
                "content": [{"type": "text", "text": f"Feedback '{feedback_id}' not found"}],
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": f"Acknowledged feedback '{feedback_id}': {action_taken}"}]
        }

    return create_sdk_mcp_server(
        "blackboard-tools",
        tools=[
            read_blackboard,
            write_blackboard,
            append_blackboard,
            get_idea_status,
            set_phase_recommendation,
            list_blackboard_files,
            declare_artifacts,
            register_feedback,
            acknowledge_feedback,
        ],
    )


def _find_artifact_owners(blackboard: Blackboard, idea_id: str, artifact_filename: str) -> list[str]:
    """Look up which agent roles own a given artifact from artifact-manifest.json."""
    import json as _json

    try:
        raw = blackboard.read_file(idea_id, "artifact-manifest.json")
        manifest_entries = _json.loads(raw)
        if not isinstance(manifest_entries, list):
            manifest_entries = [manifest_entries]
    except (FileNotFoundError, _json.JSONDecodeError):
        return []

    owners: list[str] = []
    for entry in manifest_entries:
        declared_by = entry.get("declared_by", "")
        artifacts = entry.get("artifacts", [])
        for art in artifacts:
            if isinstance(art, dict) and art.get("file") == artifact_filename:
                if declared_by and declared_by not in owners:
                    owners.append(declared_by)

    # Also include any matching role from last_serviced_by
    try:
        status = blackboard.get_status(idea_id)
        for role in status.get("last_serviced_by", {}):
            if role in owners:
                continue
            # Include roles that have previously serviced this idea
            # if they match a declaring role's phase/name pattern
    except Exception:
        pass

    return owners


def create_watcher_mcp_server(blackboard: Blackboard, idea_id: str, agent_role: str = ""):
    """Create a restricted MCP server for watcher/cadence agents.

    Exposes only read-only tools plus register_feedback — no write access.
    """

    @tool(
        "read_blackboard",
        "Read a file from the idea's blackboard directory",
        {"filename": str},
    )
    async def read_blackboard(args):
        filename = args["filename"]
        try:
            content = blackboard.read_file(idea_id, filename)
            return {"content": [{"type": "text", "text": content}]}
        except FileNotFoundError:
            return {
                "content": [{"type": "text", "text": f"File not found: {filename}"}],
                "isError": True,
            }

    @tool(
        "list_blackboard_files",
        "List all files in this idea's blackboard directory",
        {},
    )
    async def list_blackboard_files(args):
        idea_dir = blackboard.idea_dir(idea_id)
        files = [f.name for f in idea_dir.iterdir() if f.is_file()]
        return {"content": [{"type": "text", "text": "\n".join(sorted(files))}]}

    @tool(
        "get_idea_status",
        "Get the current status of this idea including phase and metadata",
        {},
    )
    async def get_idea_status(args):
        import json

        status = blackboard.get_status(idea_id)
        return {"content": [{"type": "text", "text": json.dumps(status, indent=2)}]}

    @tool(
        "register_feedback",
        (
            "Register feedback on an artifact or the idea in general. This is your "
            "ONLY write action — use it to report findings from your research. "
            "Feedback will be automatically routed to the appropriate pipeline agents."
        ),
        {
            "artifact": str,
            "comment": str,
            "severity": str,
            "pending_agents": list,
        },
    )
    async def register_feedback(args):
        import json as _json
        from datetime import datetime, timezone
        import uuid as _uuid

        artifact = args.get("artifact", "")
        comment = args.get("comment", "")
        severity = args.get("severity", "info")
        caller_pending = args.get("pending_agents", None)

        if not comment:
            return {
                "content": [{"type": "text", "text": "Comment is required"}],
                "isError": True,
            }

        identity = f"v1:agent:{agent_role}" if agent_role else "v1:agent:unknown"

        # Load existing feedback
        try:
            raw = blackboard.read_file(idea_id, "feedback.json")
            entries = _json.loads(raw)
            if not isinstance(entries, list):
                entries = []
        except (FileNotFoundError, _json.JSONDecodeError):
            entries = []

        # Determine pending_agents: caller-specified, artifact-owner lookup, or fallback
        if isinstance(caller_pending, str):
            caller_pending = [x.strip() for x in caller_pending.split(",") if x.strip()]
        if caller_pending:
            pending_agents = caller_pending
        elif artifact:
            owners = _find_artifact_owners(blackboard, idea_id, artifact)
            if owners:
                pending_agents = owners
            else:
                status = blackboard.get_status(idea_id)
                pending_agents = list(status.get("last_serviced_by", {}).keys())
        else:
            status = blackboard.get_status(idea_id)
            pending_agents = list(status.get("last_serviced_by", {}).keys())

        entry = {
            "id": str(_uuid.uuid4())[:8],
            "artifact": artifact,
            "comment": comment,
            "severity": severity,
            "from_identity": identity,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pending_agents": pending_agents,
            "acknowledged_by": [],
        }
        entries.append(entry)
        blackboard.write_file(idea_id, "feedback.json", _json.dumps(entries, indent=2))

        return {
            "content": [{"type": "text", "text": f"Feedback registered: [{severity}] {artifact} — {comment[:80]}"}]
        }

    return create_sdk_mcp_server(
        "blackboard-tools",
        tools=[
            read_blackboard,
            list_blackboard_files,
            get_idea_status,
            register_feedback,
        ],
    )
