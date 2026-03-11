"""Agent self-improvement through retrospectives."""

from __future__ import annotations

import logging
from pathlib import Path

from incubator.comms.notifications import NotificationDispatcher

logger = logging.getLogger(__name__)


class EvolutionManager:
    """Manages agent prompt evolution through retrospectives."""

    def __init__(
        self,
        project_root: Path,
        dispatcher: NotificationDispatcher,
    ) -> None:
        self.project_root = project_root
        self.dispatcher = dispatcher
        self.agents_dir = project_root / "incubator" / "agents"

    async def run_retrospective(self) -> None:
        """Analyze accumulated knowledge and propose improvements."""
        proposals = []

        for agent_dir in self.agents_dir.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
                continue

            knowledge_dir = agent_dir / "knowledge"
            learnings_path = knowledge_dir / "learnings.md"
            if not learnings_path.exists():
                continue

            learnings = learnings_path.read_text()
            if len(learnings.strip()) < 50:
                continue

            proposals.append(
                {
                    "agent": agent_dir.name,
                    "learnings_size": len(learnings),
                    "learnings_preview": learnings[:500],
                }
            )

        if not proposals:
            await self.dispatcher.notify("📊 Evolution: No learnings accumulated yet.")
            return

        summary = "📊 *Evolution Retrospective*\n\n"
        for p in proposals:
            summary += (
                f"• *{p['agent']}*: {p['learnings_size']} chars of learnings\n"
                f"  Preview: {p['learnings_preview'][:100]}...\n\n"
            )

        response = await self.dispatcher.ask(
            f"{summary}Apply evolution improvements?",
            ["approve", "skip"],
        )

        if response == "approve":
            await self.dispatcher.notify("✅ Evolution proposals approved (manual review needed)")
        else:
            await self.dispatcher.notify("⏭️ Evolution skipped")
