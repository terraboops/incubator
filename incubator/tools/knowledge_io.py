"""Knowledge Objects: semantic knowledge curation for incubator agents.

Implements the Knowledge Objects pattern — semantic hashing based on
LLM-extracted predicates from facts, stored as individual YAML files
in per-agent knowledge folders, named by hash, and retrieved at runtime.

Reference: https://arxiv.org/pdf/2603.17781
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml


def semantic_hash(predicates: list[list[str]]) -> str:
    """Compute content-addressable hash from normalized predicates.

    Two facts expressing the same knowledge ("Paris is France's capital" vs
    "France's capital is Paris") produce identical predicates after
    normalization, yielding the same hash for deduplication.
    """
    normalized = sorted(
        tuple(p[i].lower().strip() for i in range(3)) for p in predicates
    )
    content = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_object(obj: dict) -> dict:
    """Ensure required fields exist with sensible defaults."""
    now = _now_iso()
    obj.setdefault("predicates", [])
    obj.setdefault("insight", "")
    obj.setdefault("justification", "")
    obj.setdefault("idea_context", [])
    obj.setdefault("created_at", now)
    obj.setdefault("updated_at", now)
    obj.setdefault("source_agent", "")
    obj.setdefault("confidence", 0.5)
    # Compute id from predicates if missing
    if "id" not in obj and obj["predicates"]:
        obj["id"] = semantic_hash(obj["predicates"])
    return obj


def load_objects(knowledge_dir: Path) -> list[dict]:
    """Load all *.yaml knowledge objects from a directory.

    Falls back to reading learnings.md if no .yaml files exist,
    so un-migrated deployments still work.
    """
    if not knowledge_dir.exists():
        return []

    objects = []
    for f in sorted(knowledge_dir.glob("*.yaml")):
        try:
            obj = yaml.safe_load(f.read_text())
            if obj and isinstance(obj, dict):
                objects.append(_validate_object(obj))
        except (yaml.YAMLError, OSError):
            continue

    # Backward compatibility: fall back to learnings.md
    if not objects:
        md_path = knowledge_dir / "learnings.md"
        if md_path.exists():
            content = md_path.read_text().strip()
            if content:
                return [
                    _validate_object(
                        {
                            "id": "legacy",
                            "insight": content,
                            "justification": "(migrated from learnings.md — needs review)",
                            "confidence": 0.3,
                        }
                    )
                ]

    return objects


def save_object(knowledge_dir: Path, obj: dict) -> Path:
    """Write a knowledge object to {hash}.yaml.

    Returns the path of the written file.
    """
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    obj = _validate_object(obj)
    obj_id = obj.get("id") or semantic_hash(obj.get("predicates", []))
    obj["id"] = obj_id
    path = knowledge_dir / f"{obj_id}.yaml"
    path.write_text(yaml.dump(obj, default_flow_style=False, sort_keys=False, allow_unicode=True))
    return path


def delete_object(knowledge_dir: Path, obj_id: str) -> bool:
    """Delete a knowledge object by id. Returns True if deleted."""
    path = knowledge_dir / f"{obj_id}.yaml"
    if path.exists():
        path.unlink()
        return True
    return False


def find_by_id(knowledge_dir: Path, obj_id: str) -> dict | None:
    """Find a knowledge object by its hash id."""
    path = knowledge_dir / f"{obj_id}.yaml"
    if not path.exists():
        return None
    try:
        obj = yaml.safe_load(path.read_text())
        return _validate_object(obj) if obj else None
    except (yaml.YAMLError, OSError):
        return None


def format_for_prompt(objects: list[dict], max_entries: int = 20) -> str:
    """Render knowledge objects as markdown for injection into agent prompts.

    Sorted by confidence descending. Each entry shows [id] insight + justification.
    Respects max_entries budget to prevent context bloat.
    """
    if not objects:
        return ""

    sorted_objs = sorted(objects, key=lambda o: o.get("confidence", 0), reverse=True)
    sorted_objs = sorted_objs[:max_entries]

    parts = []
    for obj in sorted_objs:
        obj_id = obj.get("id", "????")
        insight = obj.get("insight", "").strip()
        justification = obj.get("justification", "").strip()
        confidence = obj.get("confidence", 0)

        entry = f"### [{obj_id}] (confidence: {confidence})\n{insight}"
        if justification:
            entry += f"\n\n**Why this matters:** {justification}"
        parts.append(entry)

    return "\n\n---\n\n".join(parts)


def search_by_predicates(
    objects: list[dict], query_predicates: list[list[str]]
) -> list[dict]:
    """Find objects whose predicates overlap with the query predicates.

    Matching: any predicate triple in common after normalization.
    """
    query_set = {
        tuple(p[i].lower().strip() for i in range(3)) for p in query_predicates
    }
    results = []
    for obj in objects:
        obj_set = {
            tuple(p[i].lower().strip() for i in range(3))
            for p in obj.get("predicates", [])
        }
        if query_set & obj_set:
            results.append(obj)
    return results


def migrate_md_to_objects(knowledge_dir: Path) -> int:
    """Split learnings.md into individual YAML objects.

    Extracted sections get empty predicates/justification (flagged for review).
    Returns count of objects created. Renames learnings.md to learnings.md.bak.
    """
    md_path = knowledge_dir / "learnings.md"
    if not md_path.exists():
        return 0

    content = md_path.read_text()
    if not content.strip():
        md_path.rename(knowledge_dir / "learnings.md.bak")
        return 0

    # Split on ## headings
    sections = re.split(r"\n(?=## )", content)
    count = 0

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Extract heading and body
        lines = section.split("\n", 1)
        heading = lines[0].lstrip("# ").strip()
        body = lines[1].strip() if len(lines) > 1 else heading

        # Generate a deterministic id from the content
        content_hash = hashlib.sha256(section.encode()).hexdigest()[:8]

        obj = {
            "id": content_hash,
            "predicates": [],
            "insight": body,
            "justification": "",
            "idea_context": [],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "source_agent": "",
            "confidence": 0.3,
        }

        # Only write if the file doesn't already exist (idempotent)
        dest = knowledge_dir / f"{content_hash}.yaml"
        if not dest.exists():
            save_object(knowledge_dir, obj)
            count += 1

    # Rename original to .bak
    bak_path = knowledge_dir / "learnings.md.bak"
    if bak_path.exists():
        bak_path.unlink()
    md_path.rename(bak_path)

    return count
