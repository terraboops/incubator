"""MCP tools for agent self-improvement and knowledge accumulation."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server


def create_evolution_mcp_server(knowledge_dir: Path):
    """Create an MCP server with evolution/learning tools."""

    @tool(
        "write_knowledge",
        "Record a learning or insight for future agent runs. "
        "Use this to capture patterns, mistakes to avoid, or effective strategies.",
        {"category": str, "content": str},
    )
    async def write_knowledge(args):
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        path = knowledge_dir / "learnings.md"
        with open(path, "a") as f:
            f.write(f"\n## {args['category']}\n{args['content']}\n")
        return {"content": [{"type": "text", "text": "Knowledge recorded"}]}

    @tool(
        "read_knowledge",
        "Read accumulated knowledge and learnings from previous runs",
        {},
    )
    async def read_knowledge(args):
        path = knowledge_dir / "learnings.md"
        if not path.exists():
            return {"content": [{"type": "text", "text": "No previous knowledge found."}]}
        return {"content": [{"type": "text", "text": path.read_text()}]}

    return create_sdk_mcp_server(
        "evolution-tools",
        tools=[write_knowledge, read_knowledge],
    )
