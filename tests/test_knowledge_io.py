"""Tests for trellis/tools/knowledge_io.py — Knowledge Objects foundation."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trellis.tools.knowledge_io import (
    delete_object,
    find_by_id,
    format_for_prompt,
    load_objects,
    migrate_md_to_objects,
    save_object,
    search_by_predicates,
    semantic_hash,
)


# ── semantic_hash ──────────────────────────────────────────────────────────


def test_semantic_hash_deterministic():
    preds = [["Paris", "is capital of", "France"]]
    assert semantic_hash(preds) == semantic_hash(preds)


def test_semantic_hash_order_independent():
    a = [["A", "r", "B"], ["C", "r", "D"]]
    b = [["C", "r", "D"], ["A", "r", "B"]]
    assert semantic_hash(a) == semantic_hash(b)


def test_semantic_hash_normalization():
    a = [["  Paris ", "IS CAPITAL OF", " france"]]
    b = [["paris", "is capital of", "france"]]
    assert semantic_hash(a) == semantic_hash(b)


def test_semantic_hash_different_content_differs():
    a = [["A", "r", "B"]]
    b = [["X", "r", "Y"]]
    assert semantic_hash(a) != semantic_hash(b)


# ── save / load / find / delete ────────────────────────────────────────────


@pytest.fixture
def knowledge_dir(tmp_path: Path) -> Path:
    d = tmp_path / "knowledge"
    d.mkdir()
    return d


def _make_obj(**overrides) -> dict:
    obj = {
        "predicates": [["agent", "must check", "calendars"]],
        "insight": "Always verify day-of-week claims.",
        "justification": "Saved 3 cycles on market-garden.",
        "idea_context": ["market-garden"],
        "source_agent": "validation",
        "confidence": 0.8,
    }
    obj.update(overrides)
    return obj


def test_save_load_roundtrip(knowledge_dir: Path):
    obj = _make_obj()
    path = save_object(knowledge_dir, obj)

    assert path.exists()
    assert path.suffix == ".yaml"

    loaded = load_objects(knowledge_dir)
    assert len(loaded) == 1
    assert loaded[0]["insight"] == obj["insight"]
    assert loaded[0]["justification"] == obj["justification"]
    assert loaded[0]["confidence"] == 0.8


def test_find_by_id(knowledge_dir: Path):
    obj = _make_obj()
    save_object(knowledge_dir, obj)
    obj_id = semantic_hash(obj["predicates"])

    found = find_by_id(knowledge_dir, obj_id)
    assert found is not None
    assert found["insight"] == obj["insight"]

    assert find_by_id(knowledge_dir, "nonexistent") is None


def test_delete_object(knowledge_dir: Path):
    obj = _make_obj()
    save_object(knowledge_dir, obj)
    obj_id = semantic_hash(obj["predicates"])

    assert delete_object(knowledge_dir, obj_id) is True
    assert find_by_id(knowledge_dir, obj_id) is None
    assert delete_object(knowledge_dir, obj_id) is False


# ── format_for_prompt ──────────────────────────────────────────────────────


def test_format_for_prompt_respects_limit(knowledge_dir: Path):
    for i in range(5):
        save_object(knowledge_dir, _make_obj(
            predicates=[[f"s{i}", "r", "o"]],
            confidence=0.5 + i * 0.1,
        ))

    objects = load_objects(knowledge_dir)
    formatted = format_for_prompt(objects, max_entries=3)
    # Should have exactly 3 entries (separated by ---)
    assert formatted.count("---") == 2  # 3 entries, 2 separators


def test_format_for_prompt_sorted_by_confidence(knowledge_dir: Path):
    save_object(knowledge_dir, _make_obj(predicates=[["low", "r", "o"]], confidence=0.3))
    save_object(knowledge_dir, _make_obj(predicates=[["high", "r", "o"]], confidence=0.9))

    objects = load_objects(knowledge_dir)
    formatted = format_for_prompt(objects)

    # The high-confidence entry should appear first
    high_pos = formatted.find("conf")
    assert "0.9" in formatted[:formatted.find("---")]


# ── search_by_predicates ──────────────────────────────────────────────────


def test_search_finds_overlap():
    objects = [
        _make_obj(predicates=[["A", "r", "B"], ["C", "r", "D"]]),
        _make_obj(predicates=[["X", "r", "Y"]]),
    ]
    results = search_by_predicates(objects, [["A", "r", "B"]])
    assert len(results) == 1
    assert results[0]["predicates"][0] == ["A", "r", "B"]


def test_search_no_overlap():
    objects = [_make_obj(predicates=[["A", "r", "B"]])]
    results = search_by_predicates(objects, [["Z", "r", "W"]])
    assert len(results) == 0


# ── migration ──────────────────────────────────────────────────────────────


def test_migrate_md_to_objects(knowledge_dir: Path):
    md = knowledge_dir / "learnings.md"
    md.write_text("## Category One\nFirst insight body.\n\n## Category Two\nSecond insight body.\n")

    count = migrate_md_to_objects(knowledge_dir)
    assert count == 2

    # learnings.md should be renamed to .bak
    assert not md.exists()
    assert (knowledge_dir / "learnings.md.bak").exists()

    # Should have 2 .yaml files
    yaml_files = list(knowledge_dir.glob("*.yaml"))
    assert len(yaml_files) == 2


def test_migrate_preserves_content(knowledge_dir: Path):
    original = "## My Heading\nThis is the body of the insight with details."
    md = knowledge_dir / "learnings.md"
    md.write_text(original)

    migrate_md_to_objects(knowledge_dir)

    objects = load_objects(knowledge_dir)
    assert len(objects) == 1
    assert "body of the insight" in objects[0]["insight"]
    # Migrated objects should have low confidence and empty predicates (flagged for review)
    assert objects[0]["confidence"] == 0.3
    assert objects[0]["predicates"] == []


def test_migrate_idempotent(knowledge_dir: Path):
    md = knowledge_dir / "learnings.md"
    md.write_text("## Test\nContent\n")

    count1 = migrate_md_to_objects(knowledge_dir)
    assert count1 == 1

    # Re-create learnings.md and migrate again
    (knowledge_dir / "learnings.md.bak").rename(md)
    count2 = migrate_md_to_objects(knowledge_dir)
    # Should not create duplicates
    assert count2 == 0

    yaml_files = list(knowledge_dir.glob("*.yaml"))
    assert len(yaml_files) == 1


# ── backward compatibility ─────────────────────────────────────────────────


def test_load_objects_fallback_to_learnings_md(knowledge_dir: Path):
    """When no .yaml files exist but learnings.md does, load as legacy object."""
    md = knowledge_dir / "learnings.md"
    md.write_text("Some raw learnings content here.")

    objects = load_objects(knowledge_dir)
    assert len(objects) == 1
    assert objects[0]["id"] == "legacy"
    assert "raw learnings content" in objects[0]["insight"]
    assert objects[0]["confidence"] == 0.3
