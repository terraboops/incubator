from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Locks older than this are considered stale regardless of PID
STALE_LOCK_TIMEOUT = timedelta(hours=2)


class LockManager:
    """File-based lock manager with PID-based stale detection.

    Ported from unfold orchestrator's lock-manager.ts pattern.
    Uses OS-level exclusive file creation for atomicity.
    """

    def __init__(self, lock_dir: Path | None = None) -> None:
        self.lock_dir = lock_dir or Path.home() / ".trellis" / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def _lock_path(self, namespace: str, lock_id: str) -> Path:
        ns_dir = self.lock_dir / namespace
        ns_dir.mkdir(parents=True, exist_ok=True)
        return ns_dir / f"{lock_id}.lock"

    def acquire(self, namespace: str, lock_id: str, executor: str = "") -> bool:
        path = self._lock_path(namespace, lock_id)

        # Check for stale lock first
        if path.exists():
            lock_data = self.get_lock_data(namespace, lock_id)
            if lock_data and self._is_stale(lock_data):
                path.unlink(missing_ok=True)
            else:
                return False

        lock_data = {
            "namespace": namespace,
            "lock_id": lock_id,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "executor": executor,
        }

        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, json.dumps(lock_data, indent=2).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False

    def release(self, namespace: str, lock_id: str) -> None:
        path = self._lock_path(namespace, lock_id)
        path.unlink(missing_ok=True)

    def is_locked(self, namespace: str, lock_id: str) -> bool:
        path = self._lock_path(namespace, lock_id)
        if not path.exists():
            return False
        lock_data = self.get_lock_data(namespace, lock_id)
        if lock_data and self._is_stale(lock_data):
            path.unlink(missing_ok=True)
            return False
        return True

    def get_lock_data(self, namespace: str, lock_id: str) -> dict | None:
        path = self._lock_path(namespace, lock_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _is_stale(lock_data: dict) -> bool:
        """A lock is stale if the PID is dead OR if it's older than the timeout."""
        pid = lock_data.get("pid", -1)
        pid_dead = False
        if pid <= 0:
            pid_dead = True
        else:
            try:
                os.kill(pid, signal.SIG_DFL)
            except ProcessLookupError:
                pid_dead = True
            except PermissionError:
                pass  # Process exists but we can't signal it

        if pid_dead:
            return True

        # PID is alive but lock might be from a long-running parent process
        started = lock_data.get("started_at", "")
        if started:
            try:
                lock_time = datetime.fromisoformat(started)
                if datetime.now(timezone.utc) - lock_time > STALE_LOCK_TIMEOUT:
                    return True
            except ValueError:
                pass

        return False
