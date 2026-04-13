"""Tests for the SurrealDB projection store."""

from __future__ import annotations

import json
import time

import pytest

from trellis.core.blackboard import Blackboard
from trellis.core.projection import ProjectionStore


@pytest.fixture
def store():
    s = ProjectionStore()
    s.connect("mem://")
    yield s
    s.close()


@pytest.fixture
def bb_with_ideas(tmp_path):
    """Create a blackboard with test ideas."""
    ideas_dir = tmp_path / "ideas"
    template = ideas_dir / "_template"
    template.mkdir(parents=True)
    (template / "status.json").write_text(
        json.dumps({"id": "", "title": "", "phase": "submitted", "phase_history": []})
    )
    bb = Blackboard(ideas_dir)
    for i in range(5):
        idea_id = f"test-idea-{i}"
        idea_dir = ideas_dir / idea_id
        idea_dir.mkdir()
        (idea_dir / "status.json").write_text(
            json.dumps(
                {
                    "id": idea_id,
                    "title": f"Test Idea {i}",
                    "phase": "ideation" if i % 2 == 0 else "released",
                    "total_cost_usd": i * 1.5,
                    "priority_score": 5.0 + i,
                    "iteration_count": i,
                    "sandbox_failure_count": 1 if i == 3 else 0,
                    "phase_history": [],
                    "pipeline": {"agents": ["ideation", "validation"], "post_ready": []},
                }
            )
        )
        # Create some agent logs
        log_dir = idea_dir / "agent-logs"
        log_dir.mkdir()
        for agent in ("ideation", "validation"):
            log_file = log_dir / f"{agent}-20260409-{100000 + i}.json"
            log_file.write_text(
                json.dumps(
                    {
                        "agent": agent,
                        "idea_id": idea_id,
                        "model": "claude-sonnet-4-6",
                        "timestamp": f"2026-04-09T0{i}:00:00Z",
                        "transcript": [{"role": "assistant"}, {"role": "result"}],
                        "session_id": f"session-{i}-{agent}",
                        "sandbox_failure": i == 3,
                    }
                )
            )
    return bb


class TestProjectionStore:
    def test_connect_and_close(self, store):
        assert store._db is not None

    def test_upsert_and_get_idea(self, store):
        store.upsert_idea(
            "my-idea", {"title": "My Idea", "phase": "ideation", "total_cost_usd": 2.0}
        )
        idea = store.get_idea("my-idea")
        assert idea is not None
        assert idea["id"] == "my-idea"
        assert idea["title"] == "My Idea"
        assert idea["phase"] == "ideation"
        assert idea["total_cost_usd"] == 2.0

    def test_upsert_overwrites(self, store):
        store.upsert_idea("x", {"title": "V1", "phase": "submitted"})
        store.upsert_idea("x", {"title": "V2", "phase": "released"})
        idea = store.get_idea("x")
        assert idea["title"] == "V2"
        assert idea["phase"] == "released"

    def test_get_ideas_for_home(self, store):
        store.upsert_idea("a", {"title": "A", "phase": "ideation"})
        store.upsert_idea("b", {"title": "B", "phase": "released"})
        ideas = store.get_ideas_for_home()
        assert len(ideas) == 2
        ids = {i["id"] for i in ideas}
        assert ids == {"a", "b"}

    def test_get_nonexistent_idea(self, store):
        assert store.get_idea("nope") is None

    def test_index_and_query_agent_logs(self, store):
        store.index_agent_log(
            "ideation",
            "idea-1",
            "ideation-001.json",
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "model": "claude-sonnet-4-6",
                "transcript": [{"role": "result"}],
            },
        )
        store.index_agent_log(
            "validation",
            "idea-1",
            "validation-001.json",
            {
                "timestamp": "2026-01-01T01:00:00Z",
                "model": "claude-sonnet-4-6",
                "transcript": [],
            },
        )
        store.index_agent_log(
            "ideation",
            "idea-2",
            "ideation-002.json",
            {
                "timestamp": "2026-01-02T00:00:00Z",
                "model": "claude-sonnet-4-6",
                "transcript": [{"role": "result"}, {"role": "assistant"}],
            },
        )

        ideation_logs = store.get_agent_logs("ideation")
        assert len(ideation_logs) == 2

        idea_logs = store.get_idea_agent_logs("idea-1")
        assert len(idea_logs) == 2

    def test_metrics(self, store):
        store.upsert_idea(
            "a", {"phase": "ideation", "total_cost_usd": 1.0, "sandbox_failure_count": 0}
        )
        store.upsert_idea(
            "b", {"phase": "released", "total_cost_usd": 3.0, "sandbox_failure_count": 2}
        )
        store.update_metrics()

        m = store.get_metrics()
        assert m is not None
        assert m["total_ideas"] == 2
        assert m["total_cost"] == 4.0
        assert m["sandbox_failure_total"] == 2
        assert m["ideas_by_phase"]["ideation"] == 1
        assert m["ideas_by_phase"]["released"] == 1

    def test_rebuild_from_blackboard(self, store, bb_with_ideas):
        store.rebuild(bb_with_ideas)
        ideas = store.get_ideas_for_home()
        assert len(ideas) == 5

        logs = store.get_agent_logs("ideation")
        assert len(logs) == 5  # one per idea

        m = store.get_metrics()
        assert m["total_ideas"] == 5

    def test_no_op_without_connection(self):
        """ProjectionStore methods are safe to call without connecting."""
        store = ProjectionStore()
        store.upsert_idea("x", {"title": "X"})
        assert store.get_ideas_for_home() == []
        assert store.get_idea("x") is None
        assert store.get_agent_logs("y") == []
        assert store.get_metrics() is None


class TestProjectionBenchmark:
    def test_read_performance(self, store, bb_with_ideas):
        """Projection reads should be <1ms per query."""
        store.rebuild(bb_with_ideas)

        t0 = time.monotonic()
        iterations = 500
        for _ in range(iterations):
            store.get_ideas_for_home()
        elapsed = time.monotonic() - t0
        ms_per_query = (elapsed / iterations) * 1000

        assert ms_per_query < 5.0, f"Home query too slow: {ms_per_query:.2f}ms"

    def test_write_performance(self, store):
        """Projection writes should be <2ms per upsert."""
        t0 = time.monotonic()
        iterations = 200
        for i in range(iterations):
            store.upsert_idea(
                f"perf-{i}",
                {
                    "title": f"Perf Test {i}",
                    "phase": "ideation",
                    "total_cost_usd": i * 0.1,
                },
            )
        elapsed = time.monotonic() - t0
        ms_per_write = (elapsed / iterations) * 1000

        assert ms_per_write < 10.0, f"Upsert too slow: {ms_per_write:.2f}ms"
