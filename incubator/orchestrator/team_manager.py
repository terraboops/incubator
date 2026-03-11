"""Agent Teams management for complex implementations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TEAM_COMPLEXITY_THRESHOLD = 5  # Number of components that triggers team mode


@dataclass
class ComplexityScore:
    score: int
    components: list[str]
    estimated_loc: int
    rationale: str


class TeamManager:
    """Assess complexity and optionally run implementation as an Agent Team."""

    def assess_complexity(self, mvp_spec: str) -> ComplexityScore:
        """Heuristic complexity assessment from MVP spec text."""
        components = []
        lines = mvp_spec.lower().split("\n")

        component_signals = [
            "frontend", "backend", "api", "database", "auth",
            "deployment", "cli", "worker", "queue", "cache",
        ]

        for signal in component_signals:
            if any(signal in line for line in lines):
                components.append(signal)

        # Estimate LOC from spec length
        estimated_loc = len(mvp_spec) * 3  # rough heuristic

        score = len(components)
        return ComplexityScore(
            score=score,
            components=components,
            estimated_loc=estimated_loc,
            rationale=f"Found {score} components: {', '.join(components)}",
        )

    def should_use_team(self, complexity: ComplexityScore) -> bool:
        """Check if we should use Agent Teams (experimental)."""
        if not os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"):
            return False
        return complexity.score >= TEAM_COMPLEXITY_THRESHOLD
