import json
from pathlib import Path

import pytest

from incubator.core.blackboard import Blackboard, slugify
from incubator.core.phase import Phase


@pytest.fixture
def bb(tmp_path: Path) -> Blackboard:
    template = tmp_path / "_template"
    template.mkdir()
    (template / "status.json").write_text("{}")
    (template / "idea.md").write_text("# Idea\n")
    (template / "research.md").write_text("# Research\n")
    return Blackboard(tmp_path)


def test_slugify():
    assert slugify("My Cool Idea!") == "my-cool-idea"
    assert slugify("  Spaces  and--dashes  ") == "spaces-and-dashes"


def test_create_idea(bb: Blackboard):
    slug = bb.create_idea("Test Idea", "A test description")
    assert slug == "test-idea"
    status = bb.get_status(slug)
    assert status["title"] == "Test Idea"
    assert status["phase"] == "submitted"
    assert status["total_cost_usd"] == 0.0


def test_create_duplicate_fails(bb: Blackboard):
    bb.create_idea("Test", "desc")
    with pytest.raises(FileExistsError):
        bb.create_idea("Test", "desc again")


def test_list_ideas(bb: Blackboard):
    bb.create_idea("Idea One", "first")
    bb.create_idea("Idea Two", "second")
    ideas = bb.list_ideas()
    assert set(ideas) == {"idea-one", "idea-two"}


def test_set_phase(bb: Blackboard):
    slug = bb.create_idea("Phase Test", "desc")
    bb.set_phase(slug, Phase.IDEATION)
    status = bb.get_status(slug)
    assert status["phase"] == "ideation"
    assert len(status["phase_history"]) == 1
    assert status["phase_history"][0]["from"] == "submitted"
    assert status["phase_history"][0]["to"] == "ideation"


def test_read_write_file(bb: Blackboard):
    slug = bb.create_idea("RW Test", "desc")
    bb.write_file(slug, "research.md", "# Research\n\nSome findings.")
    content = bb.read_file(slug, "research.md")
    assert "Some findings" in content


def test_append_file(bb: Blackboard):
    slug = bb.create_idea("Append Test", "desc")
    bb.write_file(slug, "log.md", "Line 1\n")
    bb.append_file(slug, "log.md", "Line 2\n")
    content = bb.read_file(slug, "log.md")
    assert "Line 1" in content
    assert "Line 2" in content


def test_update_status(bb: Blackboard):
    slug = bb.create_idea("Update Test", "desc")
    bb.update_status(slug, total_cost_usd=0.25, phase_recommendation="proceed")
    status = bb.get_status(slug)
    assert status["total_cost_usd"] == 0.25
    assert status["phase_recommendation"] == "proceed"
