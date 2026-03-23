"""File-based activity tracker for running agents."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "blackboard/.activity.json"


class ActivityTracker:
    """Tracks which agents are currently running via a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(_DEFAULT_PATH)

    def _read(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt activity file, resetting: %s", self.path)
        return {"running": []}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def start(self, agent_name: str, idea_id: str, idea_title: str, model: str) -> None:
        """Register an agent as running."""
        data = self._read()
        # Remove any existing entry for this agent+idea (in case of stale restart)
        data["running"] = [
            e for e in data["running"]
            if not (e["agent"] == agent_name and e["idea_id"] == idea_id)
        ]
        data["running"].append({
            "agent": agent_name,
            "idea_id": idea_id,
            "idea_title": idea_title,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
        })
        self._write(data)
        logger.info("Activity: started %s on %s", agent_name, idea_id)

    def stop(self, agent_name: str, idea_id: str) -> None:
        """Remove an agent from the running list."""
        data = self._read()
        data["running"] = [
            e for e in data["running"]
            if not (e["agent"] == agent_name and e["idea_id"] == idea_id)
        ]
        self._write(data)
        logger.info("Activity: stopped %s on %s", agent_name, idea_id)

    def get_running(self) -> list[dict]:
        """Return list of currently running agents."""
        return self._read()["running"]

    def clear_stale(self, max_age_hours: float = 2) -> int:
        """Remove entries older than max_age_hours. Returns count removed."""
        data = self._read()
        now = datetime.now(timezone.utc)
        kept = []
        removed = 0
        for entry in data["running"]:
            try:
                started = datetime.fromisoformat(entry["started_at"])
                age_hours = (now - started).total_seconds() / 3600
                if age_hours <= max_age_hours:
                    kept.append(entry)
                else:
                    removed += 1
            except (KeyError, ValueError):
                removed += 1
        if removed:
            data["running"] = kept
            self._write(data)
            logger.info("Activity: cleared %d stale entries", removed)
        return removed
