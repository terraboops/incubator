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
            "declared_by": blackboard.get_status(idea_id).get("phase", "unknown"),
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
            acknowledge_feedback,
        ],
    )
