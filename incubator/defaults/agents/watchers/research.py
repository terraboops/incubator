"""Research watcher — monitors academic research for active ideas."""

from __future__ import annotations

import logging

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

logger = logging.getLogger(__name__)


async def run_research_watcher(orchestrator) -> None:
    """Search for relevant academic research for all active ideas."""
    ideas = orchestrator.blackboard.list_ideas()

    for idea_id in ideas:
        status = orchestrator.blackboard.get_status(idea_id)
        phase = status.get("phase", "")
        if phase in ("killed", "released", "submitted"):
            continue

        idea_content = orchestrator.blackboard.read_file(idea_id, "idea.md")

        prompt = (
            f"Search for recent academic papers, preprints, or research relevant to:\n\n"
            f"{idea_content}\n\n"
            f"Focus on arxiv, Google Scholar, and research blogs. "
            f"Report any papers or findings that could impact this idea."
        )

        result_text = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                allowed_tools=["WebSearch", "WebFetch"],
                model="claude-haiku-4-5",
                max_turns=10,
                max_budget_usd=0.10,
                permission_mode="bypassPermissions",
            ),
        ):
            if isinstance(message, ResultMessage) and message.result:
                result_text = message.result

        if result_text and len(result_text) > 50:
            from datetime import datetime, timezone

            update = f"\n\n---\n_Research update {datetime.now(timezone.utc).isoformat()}_\n\n{result_text}\n"
            orchestrator.blackboard.append_file(idea_id, "research.md", update)
            await orchestrator.dispatcher.notify(
                f"📚 *{idea_id}* research update:\n{result_text[:300]}"
            )
            logger.info("Research update for %s", idea_id)
