from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from incubator.core.phase import Phase

DEFAULT_PIPELINE = {
    "stages": ["ideation", "implementation", "validation", "release"],
    "post_ready": ["competitive", "research"],
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
        status = self.get_status(idea_id)
        status.update(fields)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.write_file(idea_id, "status.json", json.dumps(status, indent=2))

    def read_file(self, idea_id: str, filename: str) -> str:
        path = self.base_dir / idea_id / filename
        if not path.exists():
            raise FileNotFoundError(f"Blackboard file not found: {path}")
        return path.read_text()

    def write_file(self, idea_id: str, filename: str, content: str) -> None:
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

    def file_exists(self, idea_id: str, filename: str) -> bool:
        return (self.base_dir / idea_id / filename).exists()

    # ── Pipeline config helpers ──────────────────────────────────────

    def get_pipeline(self, idea_id: str) -> dict:
        """Get the pipeline config for an idea, returning default if not set."""
        status = self.get_status(idea_id)
        if "pipeline" in status:
            return status["pipeline"]
        # Deep copy to prevent mutation of the module-level default
        return json.loads(json.dumps(DEFAULT_PIPELINE))

    def set_pipeline(self, idea_id: str, pipeline: dict) -> None:
        """Set the full pipeline config for an idea."""
        self.update_status(idea_id, pipeline=pipeline)

    def next_stage(self, idea_id: str) -> str | None:
        """Return the next uncompleted pipeline stage, or None if all done.

        Uses per-stage stage_results dict to determine completion:
        - No entry or "iterate" -> stage needs (re-)running
        - "proceed" -> stage is done, advance to next
        """
        pipeline = self.get_pipeline(idea_id)
        status = self.get_status(idea_id)
        serviced = status.get("last_serviced_by", {})
        stage_results = status.get("stage_results", {})

        for stage in pipeline["stages"]:
            if stage not in serviced:
                return stage
            # If this stage's result is "iterate", re-run it
            if stage_results.get(stage) == "iterate":
                return stage
            # If this stage has no result yet (serviced but no recommendation), re-run
            if stage not in stage_results:
                return stage
            # "proceed" -> this stage is done, check next
        return None

    def is_ready(self, idea_id: str) -> bool:
        """Check if all pipeline stages have been completed."""
        return self.next_stage(idea_id) is None

    def get_gating_mode(self, idea_id: str, role: str) -> str:
        """Get the gating mode for a specific agent role on this idea."""
        pipeline = self.get_pipeline(idea_id)
        gating = pipeline.get("gating", {"default": "auto", "overrides": {}})
        return gating.get("overrides", {}).get(role, gating.get("default", "auto"))

    def pipeline_has_role(self, idea_id: str, role: str) -> bool:
        """Check if a role is in this idea's pipeline (stages or post_ready)."""
        pipeline = self.get_pipeline(idea_id)
        return role in pipeline.get("stages", []) or role in pipeline.get("post_ready", [])
