"""MCP tools for structured agent knowledge accumulation.

Replaces the old append-to-markdown approach with semantic Knowledge Objects:
individual YAML files named by content hash, with predicates for deduplication
and justifications for quality control.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server

from incubator.tools.knowledge_io import (
    _now_iso,
    delete_object,
    find_by_id,
    format_for_prompt,
    load_objects,
    save_object,
    search_by_predicates,
    semantic_hash,
)


def create_evolution_mcp_server(knowledge_dir: Path):
    """Create an MCP server with structured knowledge management tools."""

    @tool(
        "write_knowledge",
        "Record a reusable insight for future runs. ONLY record knowledge that:\n"
        "- Prevents repeating a mistake that cost significant time\n"
        "- Reveals a non-obvious pattern applicable across ideas\n"
        "- Corrects a common assumption agents make\n\n"
        "Do NOT record: per-run reports, status summaries, idea-specific findings\n"
        "(those belong on the blackboard), or observations unlikely to change behavior.\n\n"
        "You MUST provide predicates (subject-relation-object triples) that capture the\n"
        "core claim. You MUST provide a justification explaining what time this saves or\n"
        "what mistake it prevents. Call read_knowledge first to check for duplicates.",
        {
            "insight": str,
            "justification": str,
            "predicates": list,
            "idea_context": str,
            "merge_with_id": str,
        },
    )
    async def write_knowledge(args):
        knowledge_dir.mkdir(parents=True, exist_ok=True)

        insight = args["insight"]
        justification = args["justification"]
        predicates = args.get("predicates", [])
        idea_context = args.get("idea_context", "")
        merge_with_id = args.get("merge_with_id", "")

        contexts = [c.strip() for c in idea_context.split(",") if c.strip()] if idea_context else []

        # Merge path: update existing object
        if merge_with_id:
            existing = find_by_id(knowledge_dir, merge_with_id)
            if existing:
                existing["insight"] = insight
                existing["justification"] = justification
                if predicates:
                    existing["predicates"] = predicates
                    existing["id"] = semantic_hash(predicates)
                if contexts:
                    existing_ctx = existing.get("idea_context", [])
                    existing["idea_context"] = list(set(existing_ctx + contexts))
                existing["confidence"] = min(1.0, existing.get("confidence", 0.5) + 0.1)
                existing["updated_at"] = _now_iso()

                # If predicates changed, we need to move the file
                if existing["id"] != merge_with_id:
                    delete_object(knowledge_dir, merge_with_id)

                path = save_object(knowledge_dir, existing)
                return _ok(f"Updated knowledge entry [{existing['id']}] at {path.name}")
            # Fall through to create new if merge target not found

        # Dedup path: check if same predicates already exist
        obj_id = semantic_hash(predicates) if predicates else ""
        if obj_id:
            existing = find_by_id(knowledge_dir, obj_id)
            if existing:
                # Same semantic hash — merge by updating content and bumping confidence
                existing["insight"] = insight
                existing["justification"] = justification
                if contexts:
                    existing_ctx = existing.get("idea_context", [])
                    existing["idea_context"] = list(set(existing_ctx + contexts))
                existing["confidence"] = min(1.0, existing.get("confidence", 0.5) + 0.1)
                existing["updated_at"] = _now_iso()
                path = save_object(knowledge_dir, existing)
                return _ok(f"Merged with existing entry [{obj_id}] (confidence bumped)")

        # Create new object
        obj = {
            "id": obj_id or hashlib.sha256(insight.encode()).hexdigest()[:8],
            "predicates": predicates,
            "insight": insight,
            "justification": justification,
            "idea_context": contexts,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "source_agent": "",
            "confidence": 0.5,
        }
        path = save_object(knowledge_dir, obj)
        return _ok(f"Created knowledge entry [{obj['id']}] at {path.name}")

    @tool(
        "read_knowledge",
        "Read all accumulated knowledge entries with their [id] prefixes. "
        "Call this BEFORE write_knowledge to check for duplicates or entries to merge.",
        {},
    )
    async def read_knowledge(args):
        objects = load_objects(knowledge_dir)
        if not objects:
            return _ok("No knowledge entries found.")
        formatted = format_for_prompt(objects, max_entries=50)
        return _ok(f"{len(objects)} entries:\n\n{formatted}")

    @tool(
        "delete_knowledge",
        "Remove a knowledge entry that is stale, wrong, or superseded. "
        "Provide the entry id (shown in [brackets] by read_knowledge).",
        {"entry_id": str},
    )
    async def delete_knowledge(args):
        entry_id = args["entry_id"]
        if delete_object(knowledge_dir, entry_id):
            return _ok(f"Deleted entry [{entry_id}]")
        return _ok(f"Entry [{entry_id}] not found")

    @tool(
        "search_knowledge",
        "Search for knowledge entries with overlapping predicates. "
        "Use this to find related knowledge before writing a new entry.",
        {"predicates": list},
    )
    async def search_knowledge(args):
        predicates = args.get("predicates", [])
        objects = load_objects(knowledge_dir)
        matches = search_by_predicates(objects, predicates)
        if not matches:
            return _ok("No matching entries found.")
        formatted = format_for_prompt(matches, max_entries=20)
        return _ok(f"{len(matches)} matching entries:\n\n{formatted}")

    return create_sdk_mcp_server(
        "evolution-tools",
        tools=[write_knowledge, read_knowledge, delete_knowledge, search_knowledge],
    )


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}
