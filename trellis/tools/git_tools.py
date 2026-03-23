"""MCP tools for git operations on the blackboard."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server


def create_git_mcp_server(repo_root: Path):
    """Create an MCP server with git tools for committing blackboard changes."""

    @tool(
        "commit_blackboard",
        "Commit current blackboard changes to git with a descriptive message",
        {"message": str},
    )
    async def commit_blackboard(args):
        import git

        try:
            repo = git.Repo(repo_root)
            repo.index.add(["blackboard/"])
            if not repo.index.diff("HEAD") and not repo.untracked_files:
                return {"content": [{"type": "text", "text": "No changes to commit"}]}
            repo.index.commit(args["message"])
            return {"content": [{"type": "text", "text": f"Committed: {args['message']}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Git error: {e}"}], "isError": True}

    return create_sdk_mcp_server("git-tools", tools=[commit_blackboard])
