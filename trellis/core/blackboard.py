from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from trellis.core.phase import Phase

DEFAULT_PIPELINE = {
    "agents": ["ideation", "implementation", "validation", "release"],
    "post_ready": ["competitive-watcher", "research-watcher"],
    "parallel_groups": [
        ["ideation", "implementation", "validation", "release"],
        ["competitive-watcher", "research-watcher"],
    ],
    "gating": {"default": "auto", "overrides": {}},
    "preset": "full-pipeline",
}


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "-", text)[:80]


class Blackboard:
    """Filesystem-based blackboard for idea state and artifacts."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.template_dir = base_dir / "_template"
        self.projection = None  # Set by app.py lifespan if projection is available

    _PROJECTION_TIMEOUT = 5  # seconds — prevents blocking on hung WS connections

    def _projection_write(self, idea_id: str, status: dict) -> None:
        """Write-through to projection cache with a timeout guard.

        If the projection store (especially ws:// mode) hangs due to a network
        partition, this ensures the calling thread is not blocked indefinitely.
        """

        def _do_write():
            try:
                self.projection.upsert_idea(idea_id, status)
                self.projection.update_metrics()
            except Exception:
                pass  # projection is best-effort

        t = threading.Thread(target=_do_write, daemon=True)
        t.start()
        t.join(timeout=self._PROJECTION_TIMEOUT)
        if t.is_alive():
            logging.getLogger(__name__).warning(
                "Projection write timed out after %ds for idea '%s'",
                self._PROJECTION_TIMEOUT,
                idea_id,
            )

    def _tracer(self):
        from trellis.otel import get_tracer

        return get_tracer()

    def create_idea(self, title: str, description: str) -> str:
        slug = slugify(title)
        idea_dir = self.base_dir / slug
        if idea_dir.exists():
            raise FileExistsError(f"Idea '{slug}' already exists")

        shutil.copytree(self.template_dir, idea_dir)

        # Write initial idea.md
        self.write_file(slug, "idea.md", f"# {title}\n\n{description}\n")

        # Initialize status
        status = {
            "id": slug,
            "title": title,
            "phase": Phase.SUBMITTED.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "phase_recommendation": None,
            "iteration_count": 0,
            "total_cost_usd": 0.0,
            "phase_history": [],
        }
        self.write_file(slug, "status.json", json.dumps(status, indent=2))
        return slug

    def list_ideas(self) -> list[str]:
        return [
            d.name
            for d in self.base_dir.iterdir()
            if d.is_dir() and d.name != "_template" and (d / "status.json").exists()
        ]

    def get_status(self, idea_id: str) -> dict:
        raw = self.read_file(idea_id, "status.json")
        return json.loads(raw)

    def set_phase(self, idea_id: str, phase: Phase) -> None:
        status = self.get_status(idea_id)
        old_phase = status["phase"]
        status["phase"] = phase.value
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        status["phase_history"].append(
            {
                "from": old_phase,
                "to": phase.value,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.write_file(idea_id, "status.json", json.dumps(status, indent=2))

    def update_status(self, idea_id: str, **fields: object) -> None:
        with self._tracer().start_as_current_span("blackboard.update_status") as span:
            span.set_attribute("idea.id", idea_id)
            span.set_attribute("fields", ",".join(fields.keys()))
            status = self.get_status(idea_id)
            status.update(fields)
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.write_file(idea_id, "status.json", json.dumps(status, indent=2))
            # Write-through to projection cache + WebSocket broadcast
            # Uses a timeout thread to prevent blocking on hung WS connections
            if self.projection:
                self._projection_write(idea_id, status)
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                from trellis.web.api.websocket import broadcast_idea_update

                loop.create_task(broadcast_idea_update(idea_id, status))
            except RuntimeError:
                pass  # no event loop running (CLI, tests)

    def read_file(self, idea_id: str, filename: str) -> str:
        path = self.base_dir / idea_id / filename
        if not path.exists():
            raise FileNotFoundError(f"Blackboard file not found: {path}")
        return path.read_text()

    def write_file(self, idea_id: str, filename: str, content: str) -> None:
        with self._tracer().start_as_current_span("blackboard.write_file") as span:
            span.set_attribute("idea.id", idea_id)
            span.set_attribute("filename", filename)
            span.set_attribute("content.bytes", len(content))
            path = self.base_dir / idea_id / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def append_file(self, idea_id: str, filename: str, content: str) -> None:
        path = self.base_dir / idea_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(content)

    def idea_dir(self, idea_id: str) -> Path:
        return self.base_dir / idea_id

    def delete_idea(self, idea_id: str) -> None:
        """Permanently delete an idea and all its artifacts."""
        import shutil

        idea_path = self.base_dir / idea_id
        if idea_path.exists() and idea_path.is_dir() and idea_id != "_template":
            shutil.rmtree(idea_path)

    def file_exists(self, idea_id: str, filename: str) -> bool:
        return (self.base_dir / idea_id / filename).exists()

    # ── Pipeline config helpers ──────────────────────────────────────

    def get_pipeline(self, idea_id: str) -> dict:
        """Get the pipeline config for an idea, returning default if not set."""
        status = self.get_status(idea_id)
        if "pipeline" in status:
            return self._migrate_pipeline(status["pipeline"])
        # Deep copy to prevent mutation of the module-level default
        return json.loads(json.dumps(DEFAULT_PIPELINE))

    def set_pipeline(self, idea_id: str, pipeline: dict) -> None:
        """Set the full pipeline config for an idea."""
        self.update_status(idea_id, pipeline=pipeline)

    def _migrate_pipeline(self, pipeline: dict) -> dict:
        """Migrate old pipeline configs to new format."""
        # Rename stages → agents
        if "stages" in pipeline and "agents" not in pipeline:
            pipeline["agents"] = pipeline.pop("stages")
        # Add parallel_groups if missing
        if "parallel_groups" not in pipeline:
            agents = pipeline.get("agents", [])
            post_ready = pipeline.get("post_ready", [])
            groups = [agents]
            if post_ready:
                groups.append(post_ready)
            pipeline["parallel_groups"] = groups
        return pipeline

    def next_agent(self, idea_id: str) -> str | None:
        """Return the next uncompleted pipeline agent, or None if all done.

        Uses per-agent stage_results dict to determine completion:
        - No entry or "iterate" -> agent needs (re-)running
        - "proceed" -> agent is done, advance to next
        """
        pipeline = self.get_pipeline(idea_id)
        status = self.get_status(idea_id)
        serviced = status.get("last_serviced_by", {})
        stage_results = status.get("stage_results", {})

        agents = pipeline.get("agents", pipeline.get("stages", []))
        for agent in agents:
            if agent not in serviced:
                return agent
            if stage_results.get(agent) == "iterate":
                return agent
            if agent not in stage_results:
                return agent
        return None

    # Keep backward compat alias
    next_stage = next_agent

    def is_ready(self, idea_id: str) -> bool:
        """Check if all pipeline agents have been completed."""
        return self.next_agent(idea_id) is None

    def pending_post_ready(self, idea_id: str) -> list[str]:
        """Return post_ready roles that haven't been serviced yet."""
        pipeline = self.get_pipeline(idea_id)
        status = self.get_status(idea_id)
        serviced = status.get("last_serviced_by", {})
        return [r for r in pipeline.get("post_ready", []) if r not in serviced]

    def get_gating_mode(self, idea_id: str, role: str) -> str:
        """Get the gating mode for a specific agent role on this idea."""
        pipeline = self.get_pipeline(idea_id)
        gating = pipeline.get("gating", {"default": "auto", "overrides": {}})
        return gating.get("overrides", {}).get(role, gating.get("default", "auto"))

    def pipeline_has_role(self, idea_id: str, role: str) -> bool:
        """Check if a role is in this idea's pipeline (agents or post_ready)."""
        pipeline = self.get_pipeline(idea_id)
        agents = pipeline.get("agents", pipeline.get("stages", []))
        return role in agents or role in pipeline.get("post_ready", [])

    # ── Feedback helpers ────────────────────────────────────────────

    def _load_feedback(self, idea_id: str) -> list[dict]:
        try:
            raw = self.read_file(idea_id, "feedback.json")
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_feedback(self, idea_id: str, entries: list[dict]) -> None:
        self.write_file(idea_id, "feedback.json", json.dumps(entries, indent=2))

    def get_pending_feedback(self, idea_id: str, role: str) -> list[dict]:
        """Return feedback entries where role is pending but hasn't acknowledged."""
        entries = self._load_feedback(idea_id)
        return [
            e
            for e in entries
            if role in e.get("pending_agents", []) and role not in e.get("acknowledged_by", [])
        ]

    def has_pending_feedback(self, idea_id: str, role: str) -> bool:
        """Quick check: does this role have unacknowledged feedback on this idea?"""
        return len(self.get_pending_feedback(idea_id, role)) > 0

    def acknowledge_feedback(self, idea_id: str, feedback_id: str, role: str) -> bool:
        """Mark a feedback entry as acknowledged by a role. Returns True if found."""
        entries = self._load_feedback(idea_id)
        for entry in entries:
            if entry.get("id") == feedback_id:
                ack = entry.setdefault("acknowledged_by", [])
                if role not in ack:
                    ack.append(role)
                self._save_feedback(idea_id, entries)
                return True
        return False
