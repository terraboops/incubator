"""Priority job queue and cadence tracking for the pool scheduler.

Design verified by TLA+ model checking (specs/pool_scheduler.tla).
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone

from croniter import croniter

# Priority constants
PRIORITY_DEFAULT = 5.0
PRIORITY_EARLY_BOOST = 1.0
MAX_BACKGROUND_PRIORITY = 4.5  # must stay below pipeline default (5.0)
FEEDBACK_PRIORITY_FACTOR = 0.9


@dataclass
class Job:
    """A unit of work for the pool scheduler."""
    priority: float           # higher = runs first
    kind: str                 # "pipeline" | "background" | "feedback"
    role: str                 # agent name from registry
    idea_id: str              # "__all__" for global agents
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class JobQueue:
    """Priority queue with (role, idea_id) deduplication.

    Uses a min-heap with negated priorities so highest priority pops first.
    The _active set prevents duplicate enqueues — a job for (role, idea_id)
    can only be queued or running once at a time.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, Job]] = []
        self._active: set[tuple[str, str]] = set()
        self._counter = itertools.count()

    def enqueue(self, job: Job) -> bool:
        """Add a job to the queue. Returns False if (role, idea_id) already active."""
        key = (job.role, job.idea_id)
        if key in self._active:
            return False
        self._active.add(key)
        heapq.heappush(self._heap, (-job.priority, next(self._counter), job))
        return True

    def pop(self) -> Job | None:
        """Remove and return the highest-priority job, or None if empty."""
        while self._heap:
            neg_pri, _seq, job = heapq.heappop(self._heap)
            key = (job.role, job.idea_id)
            if key in self._active:
                return job
            # Job was cancelled — skip it
        return None

    def peek(self) -> Job | None:
        """Return the highest-priority job without removing it."""
        while self._heap:
            neg_pri, _seq, job = self._heap[0]
            key = (job.role, job.idea_id)
            if key in self._active:
                return job
            heapq.heappop(self._heap)
        return None

    def mark_done(self, role: str, idea_id: str) -> None:
        """Mark a job as complete, allowing re-enqueue."""
        self._active.discard((role, idea_id))

    def cancel(self, role: str, idea_id: str) -> None:
        """Cancel a queued job. Lazy removal — skipped on pop."""
        self._active.discard((role, idea_id))

    @property
    def depth(self) -> int:
        """Number of active jobs (queued or running)."""
        return len(self._active)

    def __len__(self) -> int:
        return len(self._active)

    def __bool__(self) -> bool:
        return bool(self._active)


class CadenceTracker:
    """Tracks cadence timing for a background agent.

    TLA+ finding: last_run_at must be updated on ALL completions (success
    or error). Not resetting on error causes livelock — the agent stays
    permanently "due" and enters an infinite retry loop.
    """

    def __init__(self, role: str, cron_expr: str) -> None:
        self.role = role
        self.cron_expr = cron_expr
        self.last_run_at: datetime | None = None

    def elapsed_ratio(self, now: datetime | None = None) -> float:
        """How far through the cadence interval we are.

        Returns:
            0.0 = just ran
            1.0 = at cadence deadline
            >1.0 = overdue
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if self.last_run_at is None:
            return 1.0  # never run = treat as at deadline

        cron = croniter(self.cron_expr, self.last_run_at)
        next_run = cron.get_next(datetime)
        interval = next_run - self.last_run_at
        if interval.total_seconds() <= 0:
            return 1.0
        elapsed = now - self.last_run_at
        return elapsed.total_seconds() / interval.total_seconds()

    def is_due(self, now: datetime | None = None) -> bool:
        """Whether this agent should be scheduled (>= 100% through cadence)."""
        return self.elapsed_ratio(now) >= 1.0

    def priority(self, now: datetime | None = None) -> float:
        """Compute priority based on cadence urgency.

        Just ran → ~0 (no urgency)
        50% through cadence → ~2.25
        90% through cadence → ~4.05
        100%+ overdue → 4.5 (capped below pipeline default of 5.0)
        """
        ratio = self.elapsed_ratio(now)
        return min(ratio * MAX_BACKGROUND_PRIORITY, MAX_BACKGROUND_PRIORITY)


def compute_priority(
    kind: str,
    idea_priority: float = PRIORITY_DEFAULT,
    is_first_agent: bool = False,
    cadence_tracker: CadenceTracker | None = None,
    now: datetime | None = None,
) -> float:
    """Compute job priority based on kind and context.

    Pipeline: idea.priority_score + early-stage boost
    Background: cadence-ramping (0 → 10.0)
    Feedback: idea.priority_score * 0.9
    """
    if kind == "pipeline":
        priority = idea_priority
        if is_first_agent:
            priority += PRIORITY_EARLY_BOOST
        return priority
    elif kind == "background":
        if cadence_tracker:
            return cadence_tracker.priority(now)
        return PRIORITY_DEFAULT
    elif kind == "feedback":
        return idea_priority * FEEDBACK_PRIORITY_FACTOR
    return PRIORITY_DEFAULT
