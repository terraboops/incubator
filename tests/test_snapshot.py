"""Tests for BlackboardSnapshot atomicity + crash recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trellis.core.snapshot import (
    BlackboardSnapshot,
    SnapshotError,
    recover_crashed_runs,
)


@pytest.fixture
def idea_blackboard(tmp_path: Path) -> tuple[Path, str]:
    """Minimal blackboard with one idea dir holding status.json + idea.md."""
    base = tmp_path / "blackboard"
    idea_id = "test-idea"
    idea_dir = base / idea_id
    idea_dir.mkdir(parents=True)
    (idea_dir / "status.json").write_text(json.dumps({"id": idea_id, "phase": "submitted"}))
    (idea_dir / "idea.md").write_text("# Test\nbody")
    return base, idea_id


class TestSnapshotCommit:
    def test_snapshot_creates_staging_and_lock(self, idea_blackboard):
        base, idea_id = idea_blackboard
        snap = BlackboardSnapshot(base, idea_id, role="ideation", stage="prototype")

        staging = snap.snapshot()

        assert staging.exists()
        assert (staging / "status.json").read_text() == (base / idea_id / "status.json").read_text()
        lock = base / ".trellis-locks" / f"{idea_id}.lock"
        assert lock.exists()
        lock_data = json.loads(lock.read_text())
        assert lock_data["idea_id"] == idea_id
        assert lock_data["role"] == "ideation"
        assert lock_data["stage"] == "prototype"
        assert lock_data["staging_path"] == str(staging)

    def test_commit_clears_lock_and_staging(self, idea_blackboard):
        base, idea_id = idea_blackboard
        snap = BlackboardSnapshot(base, idea_id, role="ideation")
        snap.snapshot()

        snap.commit()

        assert not (base / ".trellis-locks" / f"{idea_id}.lock").exists()
        assert snap.staging is None

    def test_commit_preserves_in_run_writes(self, idea_blackboard):
        base, idea_id = idea_blackboard
        snap = BlackboardSnapshot(base, idea_id, role="ideation")
        snap.snapshot()

        # Agent writes during run
        (base / idea_id / "artifact.md").write_text("new content")
        (base / idea_id / "status.json").write_text(json.dumps({"phase": "ideation"}))

        snap.commit()

        assert (base / idea_id / "artifact.md").read_text() == "new content"
        assert json.loads((base / idea_id / "status.json").read_text())["phase"] == "ideation"


class TestRollback:
    def test_rollback_restores_baseline(self, idea_blackboard):
        base, idea_id = idea_blackboard
        snap = BlackboardSnapshot(base, idea_id, role="ideation")
        snap.snapshot()

        # Simulate agent writing garbage then crashing
        (base / idea_id / "artifact.md").write_text("garbage")
        (base / idea_id / "status.json").write_text("{}")

        snap.rollback()

        assert not (base / idea_id / "artifact.md").exists()
        status = json.loads((base / idea_id / "status.json").read_text())
        assert status["phase"] == "submitted"
        assert not (base / ".trellis-locks" / f"{idea_id}.lock").exists()
        assert snap.staging is None

    def test_rollback_is_idempotent(self, idea_blackboard):
        base, idea_id = idea_blackboard
        snap = BlackboardSnapshot(base, idea_id, role="ideation")
        snap.snapshot()
        snap.rollback()

        snap.rollback()  # second call must not raise

    def test_context_manager_commits_on_success(self, idea_blackboard):
        base, idea_id = idea_blackboard

        with BlackboardSnapshot(base, idea_id, role="ideation"):
            (base / idea_id / "out.txt").write_text("ok")

        assert (base / idea_id / "out.txt").read_text() == "ok"
        assert not (base / ".trellis-locks" / f"{idea_id}.lock").exists()

    def test_context_manager_rolls_back_on_exception(self, idea_blackboard):
        base, idea_id = idea_blackboard

        with pytest.raises(RuntimeError):
            with BlackboardSnapshot(base, idea_id, role="ideation"):
                (base / idea_id / "bad.txt").write_text("will disappear")
                raise RuntimeError("agent crashed")

        assert not (base / idea_id / "bad.txt").exists()
        assert not (base / ".trellis-locks" / f"{idea_id}.lock").exists()


class TestConcurrencyGuard:
    def test_snapshot_while_lock_exists_raises(self, idea_blackboard):
        base, idea_id = idea_blackboard
        snap1 = BlackboardSnapshot(base, idea_id, role="ideation")
        snap1.snapshot()

        snap2 = BlackboardSnapshot(base, idea_id, role="implementation")
        with pytest.raises(SnapshotError, match="Lock already held"):
            snap2.snapshot()

        snap1.commit()

    def test_snapshot_missing_idea_raises(self, tmp_path):
        base = tmp_path / "bb"
        base.mkdir()
        snap = BlackboardSnapshot(base, "nonexistent", role="ideation")

        with pytest.raises(SnapshotError, match="does not exist"):
            snap.snapshot()


class TestCrashRecovery:
    def test_recover_rolls_back_stale_lock(self, idea_blackboard):
        base, idea_id = idea_blackboard
        snap = BlackboardSnapshot(base, idea_id, role="ideation")
        snap.snapshot()
        # Simulate partial agent writes then crash (no commit/rollback)
        (base / idea_id / "partial.md").write_text("partial work")

        rolled = recover_crashed_runs(base)

        assert rolled == [idea_id]
        assert not (base / idea_id / "partial.md").exists()
        assert not (base / ".trellis-locks" / f"{idea_id}.lock").exists()

    def test_recover_no_locks_is_noop(self, idea_blackboard):
        base, _ = idea_blackboard
        assert recover_crashed_runs(base) == []

    def test_recover_gcs_orphan_staging(self, idea_blackboard, tmp_path):
        base, idea_id = idea_blackboard
        snap = BlackboardSnapshot(base, idea_id, role="ideation")
        staging = snap.snapshot()
        # Simulate crash after lock removal but before staging cleanup
        (base / ".trellis-locks" / f"{idea_id}.lock").unlink()
        assert staging.exists()

        recover_crashed_runs(base)

        assert not staging.exists()

    def test_recover_skips_lock_with_unreadable_payload(self, idea_blackboard):
        base, idea_id = idea_blackboard
        locks_dir = base / ".trellis-locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        bad_lock = locks_dir / "corrupt.lock"
        bad_lock.write_text("not json")

        recover_crashed_runs(base)

        assert not bad_lock.exists()
