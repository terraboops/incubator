"""SurrealDB-backed projection cache for fast web UI reads.

The blackboard filesystem remains the source of truth. This projection
provides instant reads by maintaining denormalized copies of idea status,
agent log indexes, and aggregate metrics.

Local dev: mem:// (in-process, rebuilt on startup)
K8s: ws://surrealdb:8000 (shared across pods)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from surrealdb import Surreal

if TYPE_CHECKING:
    from trellis.core.blackboard import Blackboard

logger = logging.getLogger(__name__)


def _clean_record_id(rid) -> str:
    """Extract clean ID string from SurrealDB RecordID."""
    s = str(rid.record_id) if hasattr(rid, "record_id") else str(rid)
    # Strip angle brackets and table prefix
    s = s.strip("⟨⟩")
    if ":" in s:
        s = s.split(":", 1)[-1].strip("⟨⟩")
    return s


SCHEMA = """
DEFINE TABLE idea SCHEMALESS;
DEFINE INDEX idx_phase ON TABLE idea COLUMNS phase;
DEFINE INDEX idx_updated ON TABLE idea COLUMNS updated_at;

DEFINE TABLE agent_log SCHEMALESS;
DEFINE INDEX idx_agent ON TABLE agent_log COLUMNS agent;
DEFINE INDEX idx_idea ON TABLE agent_log COLUMNS idea_id;
DEFINE INDEX idx_agent_idea ON TABLE agent_log COLUMNS agent, idea_id;

DEFINE TABLE metrics SCHEMALESS;
"""


class ProjectionStore:
    """Write-through projection cache backed by SurrealDB."""

    def __init__(self) -> None:
        self._db: Surreal | None = None
        self._url: str = ""

    def connect(self, url: str = "mem://") -> None:
        """Connect to SurrealDB (embedded or remote)."""
        self._url = url
        self._db = Surreal(url)
        self._db.connect()
        self._db.use("trellis", "trellis")
        # Apply schema
        for stmt in SCHEMA.strip().split("\n"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                self._db.query(stmt)
        logger.info("Projection store connected: %s", url)

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    # ── Write operations (called from blackboard hooks) ──────────────

    def upsert_idea(self, idea_id: str, status: dict) -> None:
        """Update idea projection from status dict."""
        if not self._db:
            return
        record = {
            "title": status.get("title", idea_id),
            "phase": status.get("phase", "submitted"),
            "pipeline": status.get("pipeline", {}),
            "priority_score": status.get("priority_score", 0),
            "total_cost_usd": status.get("total_cost_usd", 0),
            "iteration_count": status.get("iteration_count", 0),
            "sandbox_failure_count": status.get("sandbox_failure_count", 0),
            "updated_at": status.get("updated_at", ""),
            "created_at": status.get("created_at", ""),
            "needs_human_review": status.get("needs_human_review", False),
            "review_reason": status.get("review_reason", ""),
            "last_error": status.get("last_error", ""),
            "last_error_agent": status.get("last_error_agent", ""),
            "stage_results": status.get("stage_results", {}),
            "last_serviced_by": status.get("last_serviced_by", {}),
            "iter_counts": status.get("iter_counts", {}),
            "phase_history": status.get("phase_history", []),
            "description": status.get("description", ""),
            "sandbox_suggestions": status.get("sandbox_suggestions", []),
        }
        self._db.upsert(f"idea:`{idea_id}`", record)

    def index_agent_log(self, agent: str, idea_id: str, filename: str, metadata: dict) -> None:
        """Add/update an agent log entry in the index."""
        if not self._db:
            return
        # Use agent+idea+filename as unique key
        safe_key = filename.replace(".", "_").replace("-", "_")
        record = {
            "agent": agent,
            "idea_id": idea_id,
            "filename": filename,
            "timestamp": metadata.get("timestamp", ""),
            "model": metadata.get("model", ""),
            "transcript_len": len(metadata.get("transcript", [])),
            "cost_usd": metadata.get("cost_usd", 0),
            "session_id": metadata.get("session_id"),
            "sandbox_failure": metadata.get("sandbox_failure", False),
            "run_status": metadata.get("run_status", ""),
        }
        self._db.upsert(f"agent_log:`{safe_key}`", record)

    def update_metrics(self) -> None:
        """Recompute aggregate metrics from idea projections."""
        if not self._db:
            return
        ideas = self._db.query("SELECT phase, total_cost_usd, sandbox_failure_count FROM idea")
        phase_counts: dict[str, int] = {}
        total_cost = 0.0
        sandbox_total = 0
        for idea in ideas:
            phase = idea.get("phase", "unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            total_cost += idea.get("total_cost_usd", 0) or 0
            sandbox_total += idea.get("sandbox_failure_count", 0) or 0

        self._db.upsert(
            "metrics:current",
            {
                "ideas_by_phase": phase_counts,
                "total_cost": total_cost,
                "total_ideas": len(ideas),
                "sandbox_failure_total": sandbox_total,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    # ── Read operations (called from route handlers) ─────────────────

    def get_ideas_for_home(self) -> list[dict]:
        """All ideas with status for the home page."""
        if not self._db:
            return []
        results = self._db.query("SELECT * FROM idea ORDER BY updated_at DESC")
        # Extract clean idea_id from SurrealDB record IDs
        for r in results:
            rid = r.pop("id", None)
            if rid:
                r["id"] = _clean_record_id(rid)
        return results

    def get_idea(self, idea_id: str) -> dict | None:
        """Single idea projection."""
        if not self._db:
            return None
        results = self._db.select(f"idea:`{idea_id}`")
        if results:
            r = results if isinstance(results, dict) else results[0]
            rid = r.pop("id", None)
            if rid:
                r["id"] = _clean_record_id(rid)
            return r
        return None

    def get_agent_logs(self, agent_name: str) -> list[dict]:
        """All logs for an agent across all ideas."""
        if not self._db:
            return []
        return self._db.query(
            "SELECT * FROM agent_log WHERE agent = $agent ORDER BY timestamp DESC",
            {"agent": agent_name},
        )

    def get_idea_agent_logs(self, idea_id: str) -> list[dict]:
        """All logs for an idea across all agents."""
        if not self._db:
            return []
        return self._db.query(
            "SELECT * FROM agent_log WHERE idea_id = $idea ORDER BY timestamp DESC",
            {"idea": idea_id},
        )

    def get_metrics(self) -> dict | None:
        """Get cached aggregate metrics."""
        if not self._db:
            return None
        results = self._db.query("SELECT * FROM metrics:current")
        return results[0] if results else None

    # ── Rebuild (startup) ────────────────────────────────────────────

    def rebuild(self, blackboard: "Blackboard") -> None:
        """Full rebuild from filesystem. Called on startup for mem:// mode."""
        if not self._db:
            return

        logger.info("Rebuilding projection from blackboard...")
        idea_count = 0
        log_count = 0

        for idea_id in blackboard.list_ideas():
            try:
                status = blackboard.get_status(idea_id)
                self.upsert_idea(idea_id, status)
                idea_count += 1
            except Exception as e:
                logger.warning("Failed to project idea '%s': %s", idea_id, e)

            # Index agent logs
            log_dir = blackboard.idea_dir(idea_id) / "agent-logs"
            if log_dir.is_dir():
                for f in log_dir.iterdir():
                    if f.suffix != ".json" or f.name.startswith("."):
                        continue
                    try:
                        data = json.loads(f.read_text())
                        agent = data.get("agent", "")
                        if agent:
                            self.index_agent_log(agent, idea_id, f.name, data)
                            log_count += 1
                    except Exception:
                        pass

        self.update_metrics()
        logger.info("Projection rebuilt: %d ideas, %d agent logs", idea_count, log_count)

    def invalidate_idea(self, idea_id: str, blackboard: "Blackboard") -> None:
        """Re-read idea from filesystem and update projection."""
        if not self._db:
            return
        try:
            status = blackboard.get_status(idea_id)
            self.upsert_idea(idea_id, status)
            self.update_metrics()
        except Exception as e:
            logger.warning("Failed to invalidate idea '%s': %s", idea_id, e)
