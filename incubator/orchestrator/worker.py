"""Worker: executes a single agent run with cooperative deadline cancellation.

The deadline flow follows the spec:
1. Agent runs with deadline context in system prompt
2. At deadline - 2 minutes: interrupt agent, send "hurry up" follow-up
3. At deadline: force-terminate if still running
4. Tag run as DEADLINE or OK
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
HURRY_UP_BUFFER_SECONDS = 120  # 2 minutes before deadline
HURRY_UP_MAX_TURNS = 3


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
    """A single worker slot that executes agent runs with deadlines.

    Uses cooperative cancellation via ClaudeSDKClient.interrupt():
    - Primary run uses max_turns calculated from remaining time
    - At deadline-2min: interrupt + "save progress" follow-up query
    - At deadline: force-terminate
    """

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

    def _calculate_max_turns(self, remaining_minutes: int) -> int:
        """Calculate max_turns from remaining window time."""
        return max(MIN_TURNS, remaining_minutes * TURNS_PER_MINUTE)

    async def execute(
        self, role: str, idea_id: str, deadline: datetime
    ) -> RunResult | None:
        """Execute an agent run with cooperative deadline cancellation.

        Returns None if lock cannot be acquired.

        Flow:
        1. Acquire lock, create agent with deadline-aware prompt
        2. Run agent via ClaudeSDKClient
        3. Schedule hurry-up interrupt at deadline - 2min
        4. If agent finishes before deadline -> OK
        5. If hurry-up fires -> interrupt, send follow-up, wait for deadline
        6. If deadline fires -> force-terminate -> DEADLINE
        """
        # Global agents (phase="*") don't lock a specific idea
        if idea_id != "__all__":
            if not self.lock_manager.acquire("pool", idea_id, executor=f"worker-{self.worker_id}"):
                logger.warning("Worker %d: lock unavailable for %s", self.worker_id, idea_id)
                return None

        self.current_role = role
        self.current_idea = idea_id
        self.started_at = datetime.now(timezone.utc)
        start_time = time.monotonic()

        try:
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            remaining_minutes = max(0, int(remaining / 60))
            max_turns = self._calculate_max_turns(remaining_minutes)

            logger.info(
                "Worker %d: starting %s on %s (max_turns=%d, deadline in %dm)",
                self.worker_id, role, idea_id, max_turns, remaining_minutes,
            )

            agent = self.factory.create_agent(role)

            # Run agent -- BaseAgent.run() handles ClaudeSDKClient lifecycle,
            # including deadline context in the system prompt and interrupt support
            try:
                result = await asyncio.wait_for(
                    agent.run(idea_id, max_turns_override=max_turns, deadline=deadline),
                    timeout=remaining,
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
                    "Worker %d: %s on %s hit deadline after %.0fs",
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
            if idea_id != "__all__":
                self.lock_manager.release("pool", idea_id)
            self.current_role = None
            self.current_idea = None
            self.started_at = None
