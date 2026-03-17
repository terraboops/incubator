"""Worker: executes a single agent run with timeout-based cancellation.

Timeout comes from job_timeout_minutes in config (default 60 min),
replacing the old window-deadline approach.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from incubator.core.agent_factory import AgentFactory
from incubator.core.blackboard import Blackboard
from incubator.core.lock import LockManager

logger = logging.getLogger(__name__)

MIN_TURNS = 4
TURNS_PER_MINUTE = 2


class RunStatus(enum.Enum):
    OK = "ok"
    DEADLINE = "deadline"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class RunResult:
    status: RunStatus
    role: str
    idea_id: str
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def is_deadline(self) -> bool:
        return self.status == RunStatus.DEADLINE


class Worker:
    """A single worker slot that executes agent runs with timeouts."""

    def __init__(
        self,
        worker_id: int,
        factory: AgentFactory,
        blackboard: Blackboard,
        lock_manager: LockManager,
    ) -> None:
        self.worker_id = worker_id
        self.factory = factory
        self.blackboard = blackboard
        self.lock_manager = lock_manager
        self.current_role: str | None = None
        self.current_idea: str | None = None
        self.started_at: datetime | None = None

    @property
    def is_idle(self) -> bool:
        return self.current_role is None

    def _calculate_max_turns(self, timeout_minutes: int) -> int:
        """Calculate max_turns from timeout."""
        return max(MIN_TURNS, timeout_minutes * TURNS_PER_MINUTE)

    async def execute(self, job, timeout_seconds: float) -> RunResult | None:
        """Execute an agent run with timeout.

        Args:
            job: Job instance with role, idea_id, etc.
            timeout_seconds: Maximum time for this job in seconds.

        Returns None if lock cannot be acquired.
        """
        role = job.role
        idea_id = job.idea_id

        # Lock keyed by role:idea_id so different agents can run on the same idea
        lock_id = f"{role}:{idea_id}"
        if not self.lock_manager.acquire("pool", lock_id, executor=f"worker-{self.worker_id}"):
            logger.warning("Worker %d: lock unavailable for %s on %s", self.worker_id, role, idea_id)
            return None

        self.current_role = role
        self.current_idea = idea_id
        self.started_at = datetime.now(timezone.utc)
        start_time = time.monotonic()

        try:
            timeout_minutes = max(1, int(timeout_seconds / 60))
            max_turns = self._calculate_max_turns(timeout_minutes)
            from datetime import timedelta
            deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)

            logger.info(
                "Worker %d: starting %s on %s (max_turns=%d, timeout=%dm)",
                self.worker_id, role, idea_id, max_turns, timeout_minutes,
            )

            agent = self.factory.create_agent(role)

            try:
                result = await asyncio.wait_for(
                    agent.run(idea_id, max_turns_override=max_turns, deadline=deadline),
                    timeout=timeout_seconds,
                )
                elapsed = time.monotonic() - start_time
                return RunResult(
                    status=RunStatus.OK,
                    role=role,
                    idea_id=idea_id,
                    duration_seconds=elapsed,
                    cost_usd=result.cost_usd,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - start_time
                logger.warning(
                    "Worker %d: %s on %s hit timeout after %.0fs",
                    self.worker_id, role, idea_id, elapsed,
                )
                return RunResult(
                    status=RunStatus.DEADLINE,
                    role=role,
                    idea_id=idea_id,
                    duration_seconds=elapsed,
                    cost_usd=0.0,
                )
        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.error("Worker %d: error running %s on %s: %s", self.worker_id, role, idea_id, e)
            return RunResult(
                status=RunStatus.ERROR,
                role=role,
                idea_id=idea_id,
                duration_seconds=elapsed,
                error=str(e),
            )
        finally:
            self.lock_manager.release("pool", lock_id)
            self.current_role = None
            self.current_idea = None
            self.started_at = None
