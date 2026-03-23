"""Agent self-improvement through LLM-powered knowledge curation."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from trellis.comms.notifications import NotificationDispatcher
from trellis.tools.knowledge_io import (
    delete_object,
    load_objects,
    save_object,
)

logger = logging.getLogger(__name__)

CURATOR_SYSTEM_PROMPT = """\
You are a knowledge curator for an autonomous agent system. Your job is to
review accumulated knowledge entries and decide which to keep, merge, edit,
or drop.

For each entry, decide:
- **keep**: Entry is high-value, well-justified, broadly applicable. Optionally edit for clarity.
- **merge**: Two or more entries express overlapping knowledge. Combine into one.
- **drop**: Entry is stale, too idea-specific, has no justification, or unlikely to change behavior.

Respond with ONLY a YAML document (no markdown fences) with this structure:

actions:
  - action: keep
    id: "a1b2c3d4"
    # optional edits:
    insight: "improved wording..."
    confidence: 0.9
  - action: merge
    ids: ["a1b2c3d4", "e5f6g7h8"]
    insight: "merged insight text"
    justification: "merged justification"
    predicates:
      - ["subject", "relation", "object"]
    confidence: 0.8
  - action: drop
    id: "x9y0z1w2"
    reason: "too idea-specific, belongs on blackboard"

Guidelines:
- Quality over quantity. 10 sharp entries beat 50 noisy ones.
- Entries with empty justification should be dropped unless the insight is clearly valuable.
- Entries with empty predicates should be dropped or edited to add predicates.
- Merge entries that express the same underlying knowledge in different words.
- Prefer concise, actionable insights over verbose reports.
- Preserve the original entry ids where possible."""


class EvolutionManager:
    """Manages agent knowledge curation through LLM-powered retrospectives."""

    def __init__(
        self,
        project_root: Path,
        dispatcher: NotificationDispatcher | None = None,
    ) -> None:
        self.project_root = project_root
        self.dispatcher = dispatcher
        self.agents_dir = project_root / "agents"

    def _get_agent_knowledge(self) -> dict[str, list[dict]]:
        """Load knowledge objects for all agents."""
        result = {}
        if not self.agents_dir.exists():
            return result
        for agent_dir in sorted(self.agents_dir.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
                continue
            knowledge_dir = agent_dir / "knowledge"
            objects = load_objects(knowledge_dir)
            if objects:
                result[agent_dir.name] = objects
        return result

    def get_stats(self, agent_filter: str | None = None) -> dict[str, dict]:
        """Get knowledge statistics per agent."""
        all_knowledge = self._get_agent_knowledge()
        stats = {}
        for agent, objects in all_knowledge.items():
            if agent_filter and agent != agent_filter:
                continue
            no_justification = sum(1 for o in objects if not o.get("justification", "").strip())
            no_predicates = sum(1 for o in objects if not o.get("predicates"))
            stats[agent] = {
                "count": len(objects),
                "no_justification": no_justification,
                "no_predicates": no_predicates,
            }
        return stats

    async def run_retrospective(
        self,
        agent_filter: str | None = None,
        dry_run: bool = False,
        no_llm: bool = False,
    ) -> dict[str, list[dict]]:
        """Analyze and curate knowledge using LLM, with human approval.

        Returns dict of {agent: [actions]} that were applied (or would be, if dry_run).
        """
        all_knowledge = self._get_agent_knowledge()
        if agent_filter:
            all_knowledge = {k: v for k, v in all_knowledge.items() if k == agent_filter}

        if not all_knowledge:
            if self.dispatcher:
                await self.dispatcher.notify("[Evolution] No knowledge entries found.")
            return {}

        if no_llm:
            # Just report stats, no LLM curation
            stats = self.get_stats(agent_filter)
            if self.dispatcher:
                lines = ["[Evolution] Knowledge stats:\n"]
                for agent, s in stats.items():
                    lines.append(
                        f"  {agent}: {s['count']} entries "
                        f"({s['no_justification']} missing justification, "
                        f"{s['no_predicates']} missing predicates)"
                    )
                await self.dispatcher.notify("\n".join(lines))
            return {}

        applied: dict[str, list[dict]] = {}

        for agent, objects in all_knowledge.items():
            actions = await self._curate_agent(agent, objects, dry_run)
            if actions:
                applied[agent] = actions

        return applied

    async def _curate_agent(
        self,
        agent: str,
        objects: list[dict],
        dry_run: bool,
    ) -> list[dict]:
        """Run LLM curation on one agent's knowledge."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        # Build the curation prompt with all entries
        entries_yaml = yaml.dump(
            [
                {
                    "id": o.get("id"),
                    "predicates": o.get("predicates", []),
                    "insight": o.get("insight", ""),
                    "justification": o.get("justification", ""),
                    "idea_context": o.get("idea_context", []),
                    "confidence": o.get("confidence", 0.5),
                }
                for o in objects
            ],
            default_flow_style=False,
            allow_unicode=True,
        )

        user_prompt = (
            f"Review these {len(objects)} knowledge entries for the '{agent}' agent.\n\n"
            f"{entries_yaml}"
        )

        result_text = ""
        async for message in query(
            prompt=user_prompt,
            options=ClaudeAgentOptions(
                system_prompt=CURATOR_SYSTEM_PROMPT,
                model="claude-haiku-4-5",
                max_turns=1,
                allowed_tools=[],
            ),
        ):
            if isinstance(message, ResultMessage):
                result_text = message.result or ""

        # Parse YAML response
        actions = self._parse_curation_response(result_text)
        if not actions:
            logger.warning("No valid curation actions returned for %s", agent)
            return []

        # Build human-readable diff
        diff_lines = [f"[Evolution] Curation proposal for *{agent}* ({len(objects)} entries):\n"]
        keeps = [a for a in actions if a.get("action") == "keep"]
        merges = [a for a in actions if a.get("action") == "merge"]
        drops = [a for a in actions if a.get("action") == "drop"]
        diff_lines.append(f"  Keep: {len(keeps)}, Merge: {len(merges)}, Drop: {len(drops)}")
        for d in drops:
            diff_lines.append(f"  DROP [{d.get('id')}]: {d.get('reason', 'no reason')}")
        for m in merges:
            diff_lines.append(f"  MERGE {m.get('ids', [])} -> new entry")

        diff_text = "\n".join(diff_lines)

        if dry_run:
            if self.dispatcher:
                await self.dispatcher.notify(f"{diff_text}\n\n(dry run — no changes applied)")
            logger.info("Dry run: %s", diff_text)
            return actions

        # Ask for human approval
        if self.dispatcher:
            response = await self.dispatcher.ask(
                f"{diff_text}\n\nApply these changes?",
                ["approve", "skip"],
            )
            if response != "approve":
                await self.dispatcher.notify(f"[Evolution] Skipped curation for {agent}")
                return []

        # Apply actions
        knowledge_dir = self.agents_dir / agent / "knowledge"
        self._apply_actions(knowledge_dir, objects, actions)

        if self.dispatcher:
            await self.dispatcher.notify(
                f"[Evolution] Applied curation for {agent}: "
                f"{len(keeps)} kept, {len(merges)} merged, {len(drops)} dropped"
            )

        return actions

    def _parse_curation_response(self, text: str) -> list[dict]:
        """Parse the LLM's YAML curation response."""
        text = text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[: text.rfind("```")]

        try:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                return parsed.get("actions", [])
            return []
        except yaml.YAMLError:
            logger.warning("Failed to parse curation YAML response")
            return []

    def _apply_actions(
        self,
        knowledge_dir: Path,
        existing_objects: list[dict],
        actions: list[dict],
    ) -> None:
        """Apply curation actions to knowledge files on disk."""
        obj_by_id = {o["id"]: o for o in existing_objects}

        for action in actions:
            act = action.get("action")

            if act == "keep":
                obj_id = action.get("id")
                if obj_id not in obj_by_id:
                    continue
                obj = obj_by_id[obj_id]
                # Apply optional edits
                if "insight" in action:
                    obj["insight"] = action["insight"]
                if "confidence" in action:
                    obj["confidence"] = action["confidence"]
                from trellis.tools.knowledge_io import _now_iso
                obj["updated_at"] = _now_iso()
                save_object(knowledge_dir, obj)

            elif act == "merge":
                ids = action.get("ids", [])
                # Delete all source entries
                for mid in ids:
                    delete_object(knowledge_dir, mid)
                # Create merged entry
                from trellis.tools.knowledge_io import semantic_hash, _now_iso
                predicates = action.get("predicates", [])
                merged = {
                    "id": semantic_hash(predicates) if predicates else action.get("ids", ["merged"])[0],
                    "predicates": predicates,
                    "insight": action.get("insight", ""),
                    "justification": action.get("justification", ""),
                    "idea_context": list(
                        {
                            ctx
                            for mid in ids
                            if mid in obj_by_id
                            for ctx in obj_by_id[mid].get("idea_context", [])
                        }
                    ),
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "source_agent": action.get("source_agent", ""),
                    "confidence": action.get("confidence", 0.7),
                }
                save_object(knowledge_dir, merged)

            elif act == "drop":
                obj_id = action.get("id")
                if obj_id:
                    delete_object(knowledge_dir, obj_id)
