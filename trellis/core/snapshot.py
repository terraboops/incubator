"""Blackboard snapshot + atomic commit/rollback for role invocations.

Per the custom-stages spec: wrap every role invocation in
  snapshot → run → commit-or-rollback
so a crashing agent can't leave half-written artifacts on the blackboard.

Design choices (from the spec):
- No persistent `.git` dir in each idea — users can still `ls`/`grep` blackboard
  dirs freely. We use `git diff --no-index` only when we need a patch for
  the UI, never to track state.
- The commit point is logical: once the lock file is removed, the committed
  state is on disk. The atomic status.json rename that the scheduler
  performs afterwards is the *external* flip that makes the role "done".
- Lock files live in `<blackboard>/.trellis-locks/` (outside the idea dir)
  so they survive rollback that may rmtree the idea dir.

Crash recovery:
- On startup, scan `.trellis-locks/` — every lock found is an uncommitted
  run. Roll it back. Rollback is idempotent.
- Orphan staging dirs under `/tmp/trellis-staging-*` whose lock has already
  been cleaned are garbage-collected.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STAGING_PREFIX = "trellis-staging-"
_LOCKS_DIR = ".trellis-locks"


def _staging_root() -> Path:
    return Path(tempfile.gettempdir())


class SnapshotError(Exception):
    """Raised when snapshot/commit/rollback hit an unrecoverable error."""


class BlackboardSnapshot:
    """A single baseline→commit-or-rollback cycle around a role invocation.

    Usage::

        snap = BlackboardSnapshot(blackboard_base, idea_id, role="ideation")
        snap.snapshot()
        try:
            result = await handler.serve(ctx)
            snap.commit()  # or snap.rollback() if result.state in {iterate, failed}
        except Exception:
            snap.rollback()
            raise
    """

    def __init__(
        self,
        blackboard_base: Path,
        idea_id: str,
        *,
        role: str,
        stage: str | None = None,
    ) -> None:
        self.blackboard_base = Path(blackboard_base)
        self.idea_id = idea_id
        self.role = role
        self.stage = stage

        self.idea_dir = self.blackboard_base / idea_id
        self.locks_dir = self.blackboard_base / _LOCKS_DIR
        self.lock_path = self.locks_dir / f"{idea_id}.lock"
        self._staging: Path | None = None

    @property
    def staging(self) -> Path | None:
        return self._staging

    def snapshot(self) -> Path:
        """Copy the idea dir to `/tmp/trellis-staging-<uuid>/` and drop a lock.

        Returns the staging path. Raises SnapshotError if a lock already
        exists for this idea — a concurrent run must finish first.
        """
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            raise SnapshotError(
                f"Lock already held for idea {self.idea_id!r}; "
                "another run is in progress or crashed without rollback."
            )
        if not self.idea_dir.exists():
            raise SnapshotError(f"Idea dir does not exist: {self.idea_dir}")

        staging = _staging_root() / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
        shutil.copytree(self.idea_dir, staging)
        self._staging = staging

        lock_payload = {
            "idea_id": self.idea_id,
            "role": self.role,
            "stage": self.stage,
            "staging_path": str(staging),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(self.lock_path, lock_payload)
        logger.debug("Snapshot %s → %s", self.idea_id, staging)
        return staging

    def commit(self) -> None:
        """Release the snapshot: the current blackboard state is final.

        Order matters — we unlink the lock BEFORE cleaning staging. A crash
        between the two steps leaves only an orphan staging dir, which the
        startup scanner GCs. A crash before unlinking the lock triggers a
        rollback on next startup.
        """
        if not self.lock_path.exists():
            return  # already committed or never snapshotted
        self.lock_path.unlink(missing_ok=True)
        if self._staging and self._staging.exists():
            shutil.rmtree(self._staging, ignore_errors=True)
        self._staging = None

    def rollback(self) -> None:
        """Restore the idea dir from the staging baseline.

        Keeps the lock until rollback completes so a mid-rollback crash
        re-triggers rollback on next startup (idempotent).
        """
        staging_from_lock = self._staging or _read_staging_from_lock(self.lock_path)
        if staging_from_lock and staging_from_lock.exists():
            if self.idea_dir.exists():
                shutil.rmtree(self.idea_dir)
            shutil.copytree(staging_from_lock, self.idea_dir)
            shutil.rmtree(staging_from_lock, ignore_errors=True)
        self.lock_path.unlink(missing_ok=True)
        self._staging = None

    def __enter__(self) -> "BlackboardSnapshot":
        self.snapshot()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via a temp file + rename so readers never see a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _read_staging_from_lock(lock_path: Path) -> Path | None:
    try:
        data = json.loads(lock_path.read_text())
        staging = data.get("staging_path")
        return Path(staging) if staging else None
    except (OSError, json.JSONDecodeError):
        return None


def recover_crashed_runs(blackboard_base: Path) -> list[str]:
    """Scan for orphaned locks + staging dirs and reconcile them.

    Run on startup before the scheduler claims any work.

    Returns the list of idea IDs that were rolled back (empty if no crash
    detected). Also garbage-collects orphan `/tmp/trellis-staging-*` dirs
    whose lock has already been cleared.
    """
    rolled_back: list[str] = []
    locks_dir = Path(blackboard_base) / _LOCKS_DIR
    if locks_dir.exists():
        for lock in sorted(locks_dir.glob("*.lock")):
            idea_id = lock.stem
            logger.warning("Found stale lock for idea %r — rolling back", idea_id)
            try:
                data = json.loads(lock.read_text())
                snap = BlackboardSnapshot(
                    blackboard_base,
                    idea_id,
                    role=data.get("role", "<unknown>"),
                    stage=data.get("stage"),
                )
                snap._staging = Path(data["staging_path"]) if data.get("staging_path") else None
                snap.rollback()
                rolled_back.append(idea_id)
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                logger.error("Failed to recover lock %s: %s", lock, exc)
                lock.unlink(missing_ok=True)

    # GC orphan staging dirs (no matching lock)
    known_stagings: set[Path] = set()
    if locks_dir.exists():
        for lock in locks_dir.glob("*.lock"):
            staging = _read_staging_from_lock(lock)
            if staging:
                known_stagings.add(staging)
    for staging in _staging_root().glob(f"{_STAGING_PREFIX}*"):
        if staging not in known_stagings:
            shutil.rmtree(staging, ignore_errors=True)

    return rolled_back


__all__ = ["BlackboardSnapshot", "SnapshotError", "recover_crashed_runs"]
