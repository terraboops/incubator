"""MCP tools for blackboard read/write operations.

These are registered as custom tools on an MCP server that gets
passed to agents via the Claude Agent SDK's `mcp_servers` option.
"""

from __future__ import annotations

from claude_agent_sdk import tool, create_sdk_mcp_server

from incubator.core.blackboard import Blackboard

# Artifact files that MUST be written as .html, not .md
# Maps the markdown name to the required HTML name
REQUIRED_HTML_ARTIFACTS = {
    "research.md": "research.html",
    "competitive-analysis.md": "competitive-analysis.html",
    "feasibility.md": "feasibility.html",
    "mvp-spec.md": "mvp-spec.html",
    "implementation-log.md": "implementation-log.html",
    "validation-report.md": "validation-report.html",
    "release-plan.md": "release-plan.html",
}

ARTIFACT_REJECTION_MSG = """REJECTED: You wrote '{filename}' as markdown, but artifacts MUST be beautiful, self-contained HTML files.

Write '{html_name}' instead — a single HTML file with:
- Inline CSS with modern design (gradients, backdrop-blur, subtle animations)
- Inline SVG data visualizations (charts, diagrams, progress indicators)
- A sophisticated color palette that fits the idea's personality
- Grid/flexbox layouts, cards, visual hierarchy
- Professional consulting-quality presentation
- NO external dependencies (no CDN links, no external CSS/JS)

This is displayed as a rich embedded artifact in the dashboard. Make it gorgeous."""


def create_blackboard_mcp_server(blackboard: Blackboard, idea_id: str):
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
            "IMPORTANT: All artifact files (research, competitive-analysis, feasibility, "
            "mvp-spec, implementation-log, validation-report, release-plan) MUST be written "
            "as .html files, NOT .md. Write gorgeous, self-contained HTML with inline CSS/JS "
            "and SVG visualizations. The only allowed .md files are idea.md and feedback.md."
        ),
        {"filename": str, "content": str},
    )
    async def write_blackboard(args):
        filename = args["filename"]

        # Reject markdown writes for files that should be HTML artifacts
        if filename in REQUIRED_HTML_ARTIFACTS:
            html_name = REQUIRED_HTML_ARTIFACTS[filename]
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

        # Also reject appending to markdown artifact files
        if filename in REQUIRED_HTML_ARTIFACTS:
            html_name = REQUIRED_HTML_ARTIFACTS[filename]
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

    return create_sdk_mcp_server(
        "blackboard-tools",
        tools=[
            read_blackboard,
            write_blackboard,
            append_blackboard,
            get_idea_status,
            set_phase_recommendation,
            list_blackboard_files,
        ],
    )
