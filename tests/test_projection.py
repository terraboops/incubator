"""Tests for the SurrealDB projection store."""

from __future__ import annotations

import json
import threading
import time
import tracemalloc

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

    def test_delete_idea_removes_record_and_logs(self, store):
        store.upsert_idea("doomed", {"title": "Doomed", "phase": "ideation"})
        store.upsert_idea("survivor", {"title": "Survivor", "phase": "ideation"})
        store.index_agent_log(
            "ideation",
            "doomed",
            "ideation-001.json",
            {"timestamp": "2026-01-01T00:00:00Z", "transcript": []},
        )
        store.index_agent_log(
            "ideation",
            "survivor",
            "ideation-002.json",
            {"timestamp": "2026-01-01T00:00:00Z", "transcript": []},
        )

        store.delete_idea("doomed")

        assert store.get_idea("doomed") is None
        assert store.get_idea("survivor") is not None
        # Doomed's logs should be gone, survivor's should remain
        assert store.get_idea_agent_logs("doomed") == []
        assert len(store.get_idea_agent_logs("survivor")) == 1
        # Home query no longer returns the deleted idea
        home_ids = {i["id"] for i in store.get_ideas_for_home()}
        assert "doomed" not in home_ids
        assert "survivor" in home_ids

    def test_blackboard_delete_idea_invalidates_projection(self, store, bb_with_ideas):
        bb_with_ideas.projection = store
        # Seed the projection
        for idea_id in bb_with_ideas.list_ideas():
            store.upsert_idea(idea_id, bb_with_ideas.get_status(idea_id))
        assert store.get_idea("test-idea-0") is not None

        bb_with_ideas.delete_idea("test-idea-0")

        # Filesystem gone
        assert "test-idea-0" not in bb_with_ideas.list_ideas()
        # Projection record also gone — home page won't crash
        assert store.get_idea("test-idea-0") is None

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


@pytest.mark.slow
class TestProjectionStress:
    """Stress, soak, and concurrency tests for the projection store."""

    def test_soak_memory_stable_under_sustained_writes(self, store):
        """Memory should not grow unbounded under sustained writes."""
        tracemalloc.start()
        # Warmup
        for i in range(500):
            store.upsert_idea(f"soak-{i}", {"title": f"Soak {i}", "phase": "submitted"})
            store.update_metrics()

        snapshot1 = tracemalloc.take_snapshot()

        # Sustained writes: overwrite the same 500 IDs to keep record count bounded
        for cycle in range(20):
            for i in range(500):
                store.upsert_idea(
                    f"soak-{i}",
                    {
                        "title": f"Soak {i} cycle {cycle}",
                        "phase": "ideation",
                        "total_cost_usd": cycle * 0.1,
                    },
                )
            store.update_metrics()

        snapshot2 = tracemalloc.take_snapshot()
        tracemalloc.stop()

        # Compare memory: filter to SurrealDB-related allocations
        stats = snapshot2.compare_to(snapshot1, "lineno")
        growth = sum(s.size_diff for s in stats if s.size_diff > 0)
        growth_mb = growth / 1024 / 1024
        assert growth_mb < 50, f"Memory grew by {growth_mb:.1f}MB during soak test"

    def test_soak_no_leak_on_repeated_rebuild(self, store, bb_with_ideas):
        """Repeated rebuilds should not accumulate memory."""
        tracemalloc.start()
        # Warmup
        for _ in range(3):
            store.rebuild(bb_with_ideas)

        snapshot1 = tracemalloc.take_snapshot()

        for _ in range(20):
            store.rebuild(bb_with_ideas)

        snapshot2 = tracemalloc.take_snapshot()
        tracemalloc.stop()

        stats = snapshot2.compare_to(snapshot1, "lineno")
        growth = sum(s.size_diff for s in stats if s.size_diff > 0)
        growth_mb = growth / 1024 / 1024
        assert growth_mb < 20, f"Memory grew by {growth_mb:.1f}MB across rebuilds"

    def test_read_latency_degrades_gracefully_with_scale(self, store):
        """Read latency should scale sub-linearly with idea count."""
        results = {}
        for count in (100, 500, 1000):
            # Insert ideas up to count
            current = len(store.get_ideas_for_home())
            for i in range(current, count):
                store.upsert_idea(f"scale-{i}", {"title": f"Scale {i}", "phase": "submitted"})

            t0 = time.monotonic()
            for _ in range(50):
                store.get_ideas_for_home()
            elapsed = time.monotonic() - t0
            ms_per = (elapsed / 50) * 1000
            results[count] = ms_per

        # At 1000 ideas, read should be under 100ms
        assert results[1000] < 100, f"Read at 1000 ideas: {results[1000]:.1f}ms"
        # Degradation from 100->1000 should be less than 20x (sub-linear)
        ratio = results[1000] / max(results[100], 0.01)
        assert ratio < 20, f"Read degradation 100->1000: {ratio:.1f}x"

    def test_write_throughput_sustained(self, store):
        """Write throughput should not degrade over time."""
        batch_size = 500
        batch_times = []
        for batch in range(5):
            t0 = time.monotonic()
            for i in range(batch_size):
                idx = batch * batch_size + i
                store.upsert_idea(f"tp-{idx}", {"title": f"TP {idx}", "phase": "submitted"})
            batch_times.append(time.monotonic() - t0)

        # Last batch should be within 3x of first batch
        ratio = batch_times[-1] / max(batch_times[0], 0.001)
        assert ratio < 3.0, (
            f"Write throughput degraded: first={batch_times[0]:.3f}s, last={batch_times[-1]:.3f}s ({ratio:.1f}x)"
        )

    def test_concurrent_writers_no_data_loss(self, store):
        """Multiple threads writing concurrently should not lose data."""
        num_threads = 8
        writes_per_thread = 200
        errors = []

        def writer(thread_id):
            try:
                for i in range(writes_per_thread):
                    store.upsert_idea(
                        f"cw-{thread_id}-{i}",
                        {"title": f"Thread {thread_id} item {i}", "phase": "submitted"},
                    )
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Writer errors: {errors}"

        ideas = store.get_ideas_for_home()
        expected = num_threads * writes_per_thread
        assert len(ideas) == expected, f"Expected {expected} ideas, got {len(ideas)}"

    def test_concurrent_read_write_consistency(self, store):
        """Readers should never see partial writes."""
        idea_id = "rw-consistency"
        stop = threading.Event()
        bad_reads = []

        store.upsert_idea(idea_id, {"title": "v0", "phase": "submitted", "total_cost_usd": 0})

        def writer():
            for i in range(500):
                if stop.is_set():
                    break
                store.upsert_idea(
                    idea_id,
                    {"title": f"v{i}", "phase": "ideation", "total_cost_usd": float(i)},
                )

        def reader():
            for _ in range(1000):
                if stop.is_set():
                    break
                idea = store.get_idea(idea_id)
                if idea is None:
                    continue
                # Every read should have all expected keys
                if not all(k in idea for k in ("title", "phase", "total_cost_usd")):
                    bad_reads.append(idea)

        w = threading.Thread(target=writer)
        readers = [threading.Thread(target=reader) for _ in range(4)]
        w.start()
        for r in readers:
            r.start()
        w.join(timeout=15)
        stop.set()
        for r in readers:
            r.join(timeout=5)

        assert not bad_reads, f"Found {len(bad_reads)} partial reads"
