"""Chaos engineering tests for the SurrealDB projection cache.

These tests require a kind cluster with SurrealDB deployed.
Run with: tests/infra/setup-chaos.sh

Set SURREALDB_URL env var to point to the forwarded SurrealDB instance.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from trellis.core.blackboard import Blackboard
from trellis.core.projection import ProjectionStore

SURREALDB_URL = os.environ.get("SURREALDB_URL", "")
SKIP_REASON = "Set SURREALDB_URL to run chaos tests (e.g. ws://localhost:18000)"


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, check=check, timeout=30
    )


def _wait_for_surrealdb(timeout: float = 120):
    """Wait for SurrealDB pod to be ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _kubectl(
            "get",
            "pods",
            "-l",
            "app=surrealdb",
            "-o",
            "jsonpath={.items[0].status.phase}",
            check=False,
        )
        if result.stdout.strip() == "Running":
            return
        time.sleep(2)
    raise TimeoutError("SurrealDB pod did not become ready")


@pytest.fixture
def ws_store():
    """ProjectionStore connected to the K8s SurrealDB via WebSocket."""
    store = ProjectionStore()
    store.connect(SURREALDB_URL)
    yield store
    store.close()


@pytest.fixture
def bb_tmp(tmp_path):
    """Temporary blackboard for chaos tests."""
    ideas_dir = tmp_path / "ideas"
    template = ideas_dir / "_template"
    template.mkdir(parents=True)
    (template / "status.json").write_text(json.dumps({"phase": "submitted"}))
    return Blackboard(ideas_dir)


def _populate_ideas(store: ProjectionStore, count: int = 100) -> list[str]:
    """Populate the store with test ideas."""
    ids = []
    for i in range(count):
        idea_id = f"chaos-{i}"
        store.upsert_idea(
            idea_id,
            {
                "title": f"Chaos Test {i}",
                "phase": "ideation",
                "total_cost_usd": i * 0.1,
            },
        )
        ids.append(idea_id)
    return ids


@pytest.mark.chaos
@pytest.mark.skipif(not SURREALDB_URL, reason=SKIP_REASON)
class TestProjectionChaos:
    """Chaos tests against a real SurrealDB instance in K8s."""

    def test_ws_connection_and_basic_ops(self, ws_store):
        """Verify basic connectivity to SurrealDB over WebSocket."""
        ws_store.upsert_idea("ws-test", {"title": "WS Test", "phase": "submitted"})
        idea = ws_store.get_idea("ws-test")
        assert idea is not None
        assert idea["title"] == "WS Test"

    def test_concurrent_pod_writers(self, ws_store):
        """Multiple threads writing to the same SurrealDB — simulates multi-pod."""
        num_threads = 8
        writes_per = 100
        errors = []

        def writer(tid):
            try:
                for i in range(writes_per):
                    ws_store.upsert_idea(
                        f"mpw-{tid}-{i}",
                        {"title": f"Pod {tid} idea {i}", "phase": "ideation"},
                    )
            except Exception as e:
                errors.append((tid, str(e)))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"Writer errors: {errors}"
        ideas = ws_store.get_ideas_for_home()
        assert len(ideas) >= num_threads * writes_per

    def test_surrealdb_pod_kill_and_recover(self, ws_store):
        """Kill SurrealDB pod and verify recovery after restart."""
        # Write data
        _populate_ideas(ws_store, 50)
        assert len(ws_store.get_ideas_for_home()) >= 50

        # Kill the pod
        _kubectl("delete", "pod", "-l", "app=surrealdb", "--grace-period=0", "--force", check=False)

        # Writes should fail (or succeed if pod hasn't died yet — either is acceptable)
        time.sleep(2)
        try:
            ws_store.upsert_idea("after-kill", {"title": "After kill", "phase": "submitted"})
        except Exception:
            pass  # expected

        # Wait for pod restart
        _wait_for_surrealdb(timeout=60)
        time.sleep(5)  # grace period for port-forward to reconnect

        # Reconnect
        ws_store.close()
        ws_store.connect(SURREALDB_URL)

        # Data is gone (mem:// mode) — verify empty
        ideas = ws_store.get_ideas_for_home()
        assert len(ideas) == 0, f"Expected 0 ideas after pod kill, got {len(ideas)}"

    def test_split_brain_stale_projection(self, ws_store, bb_tmp):
        """Projection becomes stale when filesystem is updated directly."""
        idea_id = "stale-test"

        # Write via blackboard (which would normally update projection)
        bb_tmp.base_dir.mkdir(parents=True, exist_ok=True)
        idea_dir = bb_tmp.base_dir / idea_id
        idea_dir.mkdir(exist_ok=True)
        (idea_dir / "status.json").write_text(
            json.dumps(
                {
                    "title": "Stale Test",
                    "phase": "ideation",
                    "total_cost_usd": 1.0,
                }
            )
        )

        # Also write to projection
        ws_store.upsert_idea(
            idea_id, {"title": "Stale Test", "phase": "ideation", "total_cost_usd": 1.0}
        )

        # Simulate Pod B updating filesystem directly (bypassing projection)
        (idea_dir / "status.json").write_text(
            json.dumps(
                {
                    "title": "Stale Test",
                    "phase": "released",
                    "total_cost_usd": 5.0,
                }
            )
        )

        # Projection is now stale
        idea = ws_store.get_idea(idea_id)
        assert idea["phase"] == "ideation", "Projection should still show old phase"

        # invalidate_idea re-reads from filesystem
        ws_store.invalidate_idea(idea_id, bb_tmp)
        idea = ws_store.get_idea(idea_id)
        assert idea["phase"] == "released", (
            "Projection should show updated phase after invalidation"
        )

    def test_blackboard_write_survives_projection_failure(self, bb_tmp):
        """Blackboard writes must succeed even if projection is broken."""
        # Create a store with a broken connection
        bb_tmp.projection = ProjectionStore()  # not connected — all ops are no-ops

        idea_id = "bb-survive"
        idea_dir = bb_tmp.base_dir / idea_id
        idea_dir.mkdir(parents=True, exist_ok=True)
        (idea_dir / "status.json").write_text(
            json.dumps(
                {
                    "title": "Survive",
                    "phase": "submitted",
                }
            )
        )

        # This should NOT raise even though projection is broken
        bb_tmp.update_status(idea_id, phase="released")

        status = bb_tmp.get_status(idea_id)
        assert status["phase"] == "released"


@pytest.mark.chaos
@pytest.mark.skipif(not SURREALDB_URL, reason=SKIP_REASON)
class TestProjectionWsReliability:
    """WebSocket-specific reliability tests."""

    def test_rapid_reconnect(self):
        """Rapid connect/disconnect cycles should not leak resources."""
        for _ in range(20):
            store = ProjectionStore()
            store.connect(SURREALDB_URL)
            store.upsert_idea("reconnect", {"title": "Reconnect", "phase": "submitted"})
            store.close()

    def test_large_payload_over_ws(self, ws_store):
        """Large status dicts should transfer cleanly over WebSocket."""
        big_history = [
            {"from": f"phase-{i}", "to": f"phase-{i + 1}", "ts": f"2026-01-{i:02d}"}
            for i in range(10000)
        ]
        ws_store.upsert_idea(
            "big-payload",
            {
                "title": "Big Payload",
                "phase": "released",
                "phase_history": big_history,
                "total_cost_usd": 999.99,
            },
        )
        idea = ws_store.get_idea("big-payload")
        assert idea is not None
        assert idea["phase"] == "released"
