"""Tests for evolution tools behavior — tests the knowledge operations
that the MCP tools perform, via the knowledge_io layer directly.

The MCP tools in evolution_tools.py are thin wrappers around knowledge_io
functions, so we test the behavior (create, dedup, read, delete, search)
rather than the MCP protocol layer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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


@pytest.fixture
def knowledge_dir(tmp_path: Path) -> Path:
    d = tmp_path / "knowledge"
    d.mkdir()
    return d


# ── write_knowledge behavior ──────────────────────────────────────────────


def test_write_knowledge_creates_file(knowledge_dir: Path):
    """New object with unique predicates creates a .yaml file on disk."""
    predicates = [["validation", "must verify", "dates"]]
    obj = {
        "predicates": predicates,
        "insight": "Always verify dates against calendars.",
        "justification": "Saved 3 cycles on market-garden.",
        "idea_context": ["market-garden"],
        "source_agent": "validation",
        "confidence": 0.5,
    }
    path = save_object(knowledge_dir, obj)

    assert path.exists()
    assert path.suffix == ".yaml"

    loaded = yaml.safe_load(path.read_text())
    assert loaded["insight"] == "Always verify dates against calendars."
    assert loaded["justification"] == "Saved 3 cycles on market-garden."
    assert loaded["id"] == semantic_hash(predicates)


def test_write_knowledge_deduplicates(knowledge_dir: Path):
    """Same predicates produce same hash — second write overwrites the file."""
    preds = [["agent", "should check", "sources"]]
    obj_id = semantic_hash(preds)

    # First write
    save_object(knowledge_dir, {
        "id": obj_id,
        "predicates": preds,
        "insight": "First version.",
        "justification": "Reason 1.",
        "confidence": 0.5,
    })

    # Second write with same predicates — simulates dedup merge
    existing = find_by_id(knowledge_dir, obj_id)
    assert existing is not None
    existing["insight"] = "Updated version."
    existing["justification"] = "Reason 2."
    existing["confidence"] = min(1.0, existing["confidence"] + 0.1)
    save_object(knowledge_dir, existing)

    # Should still be one file (same hash)
    yaml_files = list(knowledge_dir.glob("*.yaml"))
    assert len(yaml_files) == 1

    obj = yaml.safe_load(yaml_files[0].read_text())
    assert obj["insight"] == "Updated version."
    assert obj["confidence"] == 0.6


def test_write_knowledge_requires_justification(knowledge_dir: Path):
    """Empty justification still writes — flagged in UI by empty field."""
    save_object(knowledge_dir, {
        "predicates": [["a", "b", "c"]],
        "insight": "Some insight.",
        "justification": "",
        "confidence": 0.5,
    })

    objects = load_objects(knowledge_dir)
    assert len(objects) == 1
    assert objects[0]["justification"] == ""


# ── read_knowledge behavior ───────────────────────────────────────────────


def test_read_knowledge_includes_ids(knowledge_dir: Path):
    preds = [["x", "y", "z"]]
    obj_id = semantic_hash(preds)
    save_object(knowledge_dir, {
        "id": obj_id,
        "predicates": preds,
        "insight": "Test insight.",
        "justification": "Test reason.",
        "confidence": 0.7,
    })

    objects = load_objects(knowledge_dir)
    formatted = format_for_prompt(objects)
    assert f"[{obj_id}]" in formatted


# ── delete_knowledge behavior ─────────────────────────────────────────────


def test_delete_knowledge_removes_file(knowledge_dir: Path):
    preds = [["delete", "this", "entry"]]
    obj_id = semantic_hash(preds)
    save_object(knowledge_dir, {
        "id": obj_id,
        "predicates": preds,
        "insight": "To be deleted.",
        "justification": "Reason.",
        "confidence": 0.5,
    })

    assert delete_object(knowledge_dir, obj_id)
    assert len(list(knowledge_dir.glob("*.yaml"))) == 0


# ── search_knowledge behavior ────────────────────────────────────────────


def test_search_knowledge_finds_overlap(knowledge_dir: Path):
    obj_a = {
        "predicates": [["A", "relates to", "B"]],
        "insight": "Insight A.",
        "justification": "Reason A.",
        "confidence": 0.6,
    }
    obj_x = {
        "predicates": [["X", "relates to", "Y"]],
        "insight": "Insight X.",
        "justification": "Reason X.",
        "confidence": 0.6,
    }
    save_object(knowledge_dir, obj_a)
    save_object(knowledge_dir, obj_x)

    objects = load_objects(knowledge_dir)
    results = search_by_predicates(objects, [["A", "relates to", "B"]])

    assert len(results) == 1
    assert "Insight A" in results[0]["insight"]
