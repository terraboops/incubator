from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    SUBMITTED = "submitted"
    IDEATION = "ideation"
    IDEATION_REVIEW = "ideation_review"
    IMPLEMENTATION = "implementation"
    IMPLEMENTATION_REVIEW = "implementation_review"
    VALIDATION = "validation"
    VALIDATION_REVIEW = "validation_review"
    RELEASE = "release"
    RELEASED = "released"
    KILLED = "killed"
    PAUSED = "paused"


VALID_TRANSITIONS: dict[Phase, list[Phase]] = {
    Phase.SUBMITTED: [Phase.IDEATION, Phase.KILLED],
    Phase.IDEATION: [Phase.IDEATION_REVIEW, Phase.KILLED],
    Phase.IDEATION_REVIEW: [Phase.IMPLEMENTATION, Phase.IDEATION, Phase.KILLED, Phase.PAUSED],
    Phase.IMPLEMENTATION: [Phase.IMPLEMENTATION_REVIEW, Phase.KILLED],
    Phase.IMPLEMENTATION_REVIEW: [
        Phase.VALIDATION,
        Phase.IMPLEMENTATION,
        Phase.KILLED,
        Phase.PAUSED,
    ],
    Phase.VALIDATION: [Phase.VALIDATION_REVIEW, Phase.KILLED],
    Phase.VALIDATION_REVIEW: [
        Phase.RELEASE,
        Phase.IMPLEMENTATION,
        Phase.KILLED,
        Phase.PAUSED,
    ],
    Phase.RELEASE: [Phase.RELEASED, Phase.KILLED],
    # Released ideas loop back through the full pipeline for refinement
    Phase.RELEASED: [Phase.IDEATION, Phase.KILLED],
    Phase.KILLED: [Phase.SUBMITTED],
    Phase.PAUSED: [Phase.IDEATION, Phase.IMPLEMENTATION, Phase.VALIDATION, Phase.RELEASE],
}

# Map from review phase to the agent phase that precedes it
REVIEW_TO_AGENT_PHASE: dict[Phase, Phase] = {
    Phase.IDEATION_REVIEW: Phase.IDEATION,
    Phase.IMPLEMENTATION_REVIEW: Phase.IMPLEMENTATION,
    Phase.VALIDATION_REVIEW: Phase.VALIDATION,
}

# Map from review phase to the next phase if approved
REVIEW_TO_NEXT_PHASE: dict[Phase, Phase] = {
    Phase.IDEATION_REVIEW: Phase.IMPLEMENTATION,
    Phase.IMPLEMENTATION_REVIEW: Phase.VALIDATION,
    Phase.VALIDATION_REVIEW: Phase.RELEASE,
}


def can_transition(from_phase: Phase, to_phase: Phase) -> bool:
    return to_phase in VALID_TRANSITIONS.get(from_phase, [])


def get_agent_phase(phase: Phase) -> Phase | None:
    """Return the phase enum value that requires an agent run (non-review phases)."""
    if phase in (Phase.IDEATION, Phase.IMPLEMENTATION, Phase.VALIDATION, Phase.RELEASE):
        return phase
    return None
