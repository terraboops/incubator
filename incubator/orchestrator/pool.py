"""PoolManager: cycle clock, role rotation, scheduling, state snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from incubator.config import Settings
from incubator.core.blackboard import Blackboard
from incubator.core.lock import LockManager
from incubator.core.registry import load_registry
from incubator.orchestrator.worker import Worker, RunResult, RunStatus

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 10
PRIORITY_DEFAULT = 5.0
PRIORITY_EARLY_BOOST = 1.0
STARVATION_THRESHOLD = 0.5
DEADLINE_WARNING_THRESHOLD = 2
MAX_ITERATE_PER_STAGE = 3


@dataclass
class WindowState:
    """Tracks state within a single cycle window."""
    started_at: datetime
    cycle_time_minutes: int
    serviced: set[tuple[str, str]] = field(default_factory=set)

    @property
    def deadline(self) -> datetime:
        return self.started_at + timedelta(minutes=self.cycle_time_minutes)

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.deadline

    @property
    def remaining_seconds(self) -> float:
        return max(0, (self.deadline - datetime.now(timezone.utc)).total_seconds())

    def mark_serviced(self, role: str, idea_id: str) -> None:
        self.serviced.add((role, idea_id))

    def is_serviced(self, role: str, idea_id: str) -> bool:
        return (role, idea_id) in self.serviced


@dataclass
class RoleHealth:
    """Tracks expected vs actual runs for a role over 24h."""
    expected: int = 0
    actual: int = 0

    @property
    def ratio(self) -> float:
        return self.actual / self.expected if self.expected > 0 else 1.0

    @property
    def is_starved(self) -> bool:
        return self.expected >= 3 and self.ratio < STARVATION_THRESHOLD


@dataclass
class PoolState:
    """Serializable snapshot of pool state for web UI and crash recovery."""
    pool_size: int
    cycle_time_minutes: int
    window: WindowState | None
    workers: list[dict]
    role_health: dict[str, dict]
    deadline_counts: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "pool_size": self.pool_size,
            "cycle_time_minutes": self.cycle_time_minutes,
            "current_window": {
                "started_at": self.window.started_at.isoformat() if self.window else None,
                "serviced": [
                    {"role": r, "idea_id": i} for r, i in (self.window.serviced if self.window else [])
                ],
                "remaining_seconds": self.window.remaining_seconds if self.window else 0,
            },
            "workers": self.workers,
            "role_health": self.role_health,
            "deadline_counts": self.deadline_counts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


class PoolManager:
    """Manages the worker pool lifecycle and scheduling."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.blackboard = Blackboard(settings.blackboard_dir)
        self.lock_manager = LockManager()
        self.registry = load_registry(settings.registry_path)

        # Derive roles from registry
        self.roles = [a.name for a in self.registry.agents.values() if a.status == "active"]

        # State
        self.window: WindowState | None = None
        self.workers: list[Worker] = []
        self.role_health: dict[str, RoleHealth] = defaultdict(RoleHealth)
        self.deadline_counts: dict[str, int] = defaultdict(int)
        self._running = False

        # Pool dir for snapshots
        self.pool_dir = settings.project_root / "pool"
        self.pool_dir.mkdir(exist_ok=True)

    # Phases where the pool should not schedule work
    _TERMINAL_PHASES = {"killed", "released", "paused"}

    def _get_active_ideas(self) -> list[dict]:
        """Get all non-terminal ideas sorted by priority."""
        ideas = []
        for idea_id in self.blackboard.list_ideas():
            status = self.blackboard.get_status(idea_id)
            phase = status.get("phase", "submitted")
            if phase in self._TERMINAL_PHASES:
                # Allow released ideas that still have post_ready work
                if phase != "released" or not self.blackboard.pending_post_ready(idea_id):
                    continue
            # Skip review phases — these need human/gating decisions, not agent runs
            if phase.endswith("_review"):
                continue
            # Skip ideas awaiting human review when Telegram isn't configured
            # (no way to resolve the review without a human channel)
            if status.get("needs_human_review") and not self.settings.telegram_bot_token:
                continue
            # Skip ideas without pipeline config (pre-pool legacy ideas)
            if "pipeline" not in status:
                continue
            # Apply early-stage boost
            score = status.get("priority_score", PRIORITY_DEFAULT)
            if phase in ("submitted", "ideation"):
                score += PRIORITY_EARLY_BOOST
            status["_effective_priority"] = score
            ideas.append(status)
        ideas.sort(key=lambda s: s.get("_effective_priority", 0), reverse=True)
        return ideas

    def _max_concurrent(self, role: str) -> int:
        """Get the max concurrent instances for a role from registry."""
        agent_config = self.registry.get_agent(role)
        return agent_config.max_concurrent if agent_config else 1

    def _build_work_queue(
        self,
        ideas: list[dict],
        serviced: set[tuple[str, str]],
        locked: set[str],
    ) -> list[tuple[str, str]]:
        """Build ordered list of (role, idea_id) assignments.

        Idea-first scheduling: iterate ideas in priority order and assign
        each idea's next pipeline stage. This ensures the highest-priority
        ideas always get scheduled first regardless of which role they need.

        Constraints:
        - Each idea can be processed by at most 1 agent at a time.
        - Each role has a max_concurrent limit (how many copies can run).
        - Each (role, idea_id) pair is only scheduled once per window.

        Three passes:
        1. Pipeline work — assign each idea's next stage in priority order.
        2. Feedback work — assign feedback-pending roles for remaining capacity.
        3. Global agents (phase="*") — run against all ideas with __all__ sentinel.
        """
        queue: list[tuple[str, str]] = []
        queued_ideas: set[str] = set()  # 1 agent per idea at a time
        role_counts: dict[str, int] = defaultdict(int)

        # Pass 1: pipeline stage work (idea-first)
        for idea in ideas:
            idea_id = idea["id"]

            if idea_id in locked or idea_id in queued_ideas:
                continue

            if self.blackboard.is_ready(idea_id):
                # Main stages done — schedule first pending post_ready role
                for post_role in self.blackboard.pending_post_ready(idea_id):
                    if (post_role, idea_id) in serviced:
                        continue
                    if post_role not in self.roles:
                        continue
                    if role_counts[post_role] >= self._max_concurrent(post_role):
                        continue
                    queue.append((post_role, idea_id))
                    queued_ideas.add(idea_id)
                    role_counts[post_role] += 1
                    break  # 1 agent per idea at a time
                continue

            next_role = self.blackboard.next_stage(idea_id)
            if not next_role:
                continue

            if (next_role, idea_id) in serviced:
                continue

            if role_counts[next_role] >= self._max_concurrent(next_role):
                continue

            queue.append((next_role, idea_id))
            queued_ideas.add(idea_id)
            role_counts[next_role] += 1

        # Pass 2: feedback-driven work (idea-first, cross-cutting)
        for idea in ideas:
            idea_id = idea["id"]

            if idea_id in locked or idea_id in queued_ideas:
                continue

            # Find first role with pending feedback that has capacity
            for role in self.roles:
                if (role, idea_id) in serviced:
                    continue
                if role_counts[role] >= self._max_concurrent(role):
                    continue
                if self.blackboard.has_pending_feedback(idea_id, role):
                    queue.append((role, idea_id))
                    queued_ideas.add(idea_id)
                    role_counts[role] += 1
                    break  # 1 agent per idea at a time

        # Pass 2: global agents (phase="*") run against all ideas at once
        for role in self.roles:
            config = self.registry.get_agent(role)
            if not config or config.phase != "*":
                continue
            if config.status != "active":
                continue
            if (role, "__all__") not in serviced and ideas:
                queue.append((role, "__all__"))

        return queue

    def _snapshot(self) -> None:
        """Write pool state to filesystem for web UI and crash recovery."""
        worker_data = []
        for w in self.workers:
            if w.is_idle:
                worker_data.append({"id": w.worker_id, "status": "idle"})
            else:
                elapsed = (datetime.now(timezone.utc) - w.started_at).total_seconds() if w.started_at else 0
                worker_data.append({
                    "id": w.worker_id,
                    "status": "active",
                    "role": w.current_role,
                    "idea": w.current_idea,
                    "started_at": w.started_at.isoformat() if w.started_at else None,
                    "elapsed_seconds": elapsed,
                })

        state = PoolState(
            pool_size=self.settings.pool_size,
            cycle_time_minutes=self.settings.cycle_time_minutes,
            window=self.window,
            workers=worker_data,
            role_health={k: {"expected": v.expected, "actual": v.actual} for k, v in self.role_health.items()},
            deadline_counts=dict(self.deadline_counts),
        )

        # Atomic write
        state_path = self.pool_dir / "state.json"
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.pool_dir, suffix=".tmp")
        try:
            with open(tmp_fd, "w") as f:
                json.dump(state.to_dict(), f, indent=2)
            Path(tmp_path).replace(state_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    async def _handle_result(self, result: RunResult) -> None:
        """Process a completed worker run -- update tracking, apply gating."""
        if result.idea_id == "__all__":
            # Global agents: only update role health, skip idea-specific tracking
            if result.status in (RunStatus.OK, RunStatus.DEADLINE):
                self.role_health[result.role].actual += 1
            return

        if result.status == RunStatus.DEADLINE:
            self.deadline_counts[result.role] = self.deadline_counts.get(result.role, 0) + 1
            # Log deadline hit to idea status
            status = self.blackboard.get_status(result.idea_id)
            hits = status.get("deadline_hits", {})
            hits[result.role] = hits.get(result.role, 0) + 1
            self.blackboard.update_status(result.idea_id, deadline_hits=hits)

        if result.status in (RunStatus.OK, RunStatus.DEADLINE):
            # Update role health
            self.role_health[result.role].actual += 1

            # Update last_serviced_by
            status = self.blackboard.get_status(result.idea_id)
            serviced = status.get("last_serviced_by", {})
            serviced[result.role] = datetime.now(timezone.utc).isoformat()
            self.blackboard.update_status(
                result.idea_id,
                last_serviced_by=serviced,
                total_cost_usd=status.get("total_cost_usd", 0) + result.cost_usd,
                iteration_count=status.get("iteration_count", 0) + 1,
            )

            # Bridge: copy agent's phase_recommendation into stage_results[role]
            # so the pool's next_stage() logic can detect completion.
            status = self.blackboard.get_status(result.idea_id)
            recommendation = status.get("phase_recommendation", "")
            if recommendation in ("proceed", "iterate", "kill"):
                stage_results = status.get("stage_results", {})
                stage_results[result.role] = recommendation
                self.blackboard.update_status(result.idea_id, stage_results=stage_results)

                # Advance the phase field to match the current/next pipeline stage
                # so the pool's _get_active_ideas() doesn't filter it out.
                if recommendation == "proceed":
                    next_stage = self.blackboard.next_stage(result.idea_id)
                    if next_stage:
                        old_phase = status.get("phase", "submitted")
                        if old_phase != next_stage:
                            history = status.get("phase_history", [])
                            history.append({
                                "from": old_phase,
                                "to": next_stage,
                                "at": datetime.now(timezone.utc).isoformat(),
                            })
                            self.blackboard.update_status(
                                result.idea_id,
                                phase=next_stage,
                                phase_history=history,
                            )
                            logger.info("Idea '%s' advanced: %s -> %s", result.idea_id, old_phase, next_stage)
                    elif self.blackboard.is_ready(result.idea_id):
                        # All stages done — mark as released
                        old_phase = status.get("phase", "submitted")
                        history = status.get("phase_history", [])
                        prior_releases = sum(1 for e in history if e.get("to") == "released")
                        history.append({
                            "from": old_phase,
                            "to": "released",
                            "at": datetime.now(timezone.utc).isoformat(),
                        })
                        max_refinement_cycles = status.get("max_refinement_cycles", 1)
                        if max_refinement_cycles == 0 or prior_releases < max_refinement_cycles:
                            # Loop back for another refinement cycle
                            self.blackboard.update_status(
                                result.idea_id,
                                phase="submitted",
                                phase_history=history,
                                stage_results={},
                            )
                            logger.info(
                                "Idea '%s' completed pipeline (release %d/%d), looping for refinement",
                                result.idea_id, prior_releases + 1, max_refinement_cycles,
                            )
                        else:
                            # Hit the refinement cap — mark terminal
                            self.blackboard.update_status(
                                result.idea_id,
                                phase="released",
                                phase_history=history,
                            )
                            logger.info(
                                "Idea '%s' reached max refinement cycles (%d), marking terminal",
                                result.idea_id, max_refinement_cycles,
                            )
                elif recommendation == "kill":
                    self.blackboard.update_status(result.idea_id, phase="killed")
                    logger.info("Idea '%s' killed by %s", result.idea_id, result.role)

            # Apply gating
            await self._apply_gating(result)

        # Broadcast event
        try:
            from incubator.web.api.websocket import broadcast_event
            await broadcast_event("worker_done", {
                "worker_id": result.role,
                "idea_id": result.idea_id,
                "status": result.status.value,
                "duration": result.duration_seconds,
            })
        except Exception:
            pass

    async def _apply_gating(self, result: RunResult) -> None:
        """Apply gating logic after an agent run completes."""
        gating_mode = self.blackboard.get_gating_mode(result.idea_id, result.role)
        status = self.blackboard.get_status(result.idea_id)
        stage_results = status.get("stage_results", {})
        recommendation = stage_results.get(result.role) or status.get("phase_recommendation", "proceed")

        if gating_mode == "auto":
            if recommendation == "iterate":
                # Check iteration cap
                iteration_count = status.get("iteration_count", 0)
                if iteration_count >= MAX_ITERATE_PER_STAGE:
                    logger.warning(
                        "Max iterations reached for %s on %s, escalating to human review",
                        result.role, result.idea_id,
                    )
                    self.blackboard.update_status(
                        result.idea_id, needs_human_review=True,
                        review_reason=f"{result.role} hit max iterations ({MAX_ITERATE_PER_STAGE})",
                    )
            # Auto mode: recommendation already set, pool will pick up next stage
        elif gating_mode == "llm-decides":
            if recommendation == "needs_review":
                self.blackboard.update_status(
                    result.idea_id, needs_human_review=True,
                    review_reason=status.get("phase_reasoning", "Agent flagged uncertainty"),
                )
        elif gating_mode == "human-review":
            self.blackboard.update_status(
                result.idea_id, needs_human_review=True,
                review_reason=f"Human review required after {result.role}",
            )

    def _recover_from_snapshot(self) -> None:
        """Crash recovery: read pool/state.json and clean up stale state.

        1. Release locks from any workers marked "running" (they're dead)
        2. If the snapshot's window is still valid, resume it
        3. Force-release any locks older than 2 * cycle_time
        """
        state_path = self.pool_dir / "state.json"
        if not state_path.exists():
            logger.info("No pool state snapshot found, starting fresh")
            return

        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read pool state snapshot: %s", e)
            return

        # Release locks from dead workers
        for w in state.get("workers", []):
            if not w.get("idle", True) and w.get("idea_id"):
                logger.info("Releasing stale lock for %s from crashed worker %d",
                           w["idea_id"], w.get("id", 0))
                self.lock_manager.release("pool", w["idea_id"])

        # Check if we can resume the window
        window_data = state.get("current_window", {})
        started_at_str = window_data.get("started_at")
        if started_at_str:
            started_at = datetime.fromisoformat(started_at_str)
            potential_window = WindowState(
                started_at=started_at,
                cycle_time_minutes=self.settings.cycle_time_minutes,
            )
            if not potential_window.is_expired:
                # Resume this window
                for pair in window_data.get("serviced", []):
                    potential_window.mark_serviced(pair["role"], pair["idea_id"])
                self.window = potential_window
                logger.info("Resuming window from snapshot (%.0fs remaining)",
                           potential_window.remaining_seconds)

        # Restore role health and deadline counters
        for role, health in state.get("role_health", {}).items():
            self.role_health[role] = RoleHealth(
                expected=health.get("expected", 0),
                actual=health.get("actual", 0),
            )
        self.deadline_counts = defaultdict(int, state.get("deadline_counts", {}))

    async def _rescore_priorities(self) -> None:
        """Re-score priorities for all active ideas using the orchestrator's scoring."""
        try:
            from incubator.orchestrator.orchestrator import Orchestrator
            orchestrator = Orchestrator.__new__(Orchestrator)
            orchestrator.blackboard = self.blackboard
            orchestrator.settings = self.settings
            await orchestrator.score_priorities()
            logger.info("Priority scores updated")
        except Exception as e:
            logger.warning("Priority re-scoring failed: %s", e)

    async def run(self) -> None:
        """Main pool loop. Runs until stopped."""
        self._running = True
        logger.info("Pool starting with %d workers, %dm cycle time",
                     self.settings.pool_size, self.settings.cycle_time_minutes)

        # Create workers
        from incubator.core.agent_factory import AgentFactory
        from incubator.comms.notifications import NotificationDispatcher
        from incubator.comms.telegram import TelegramNotifier

        telegram = TelegramNotifier(self.settings.telegram_bot_token, self.settings.telegram_chat_id)
        dispatcher = NotificationDispatcher(telegram)
        factory = AgentFactory(
            registry=self.registry,
            blackboard=self.blackboard,
            dispatcher=dispatcher,
            project_root=self.settings.project_root,
        )

        self.workers = [
            Worker(i + 1, factory, self.blackboard, self.lock_manager)
            for i in range(self.settings.pool_size)
        ]

        # Crash recovery: clean up stale state from previous run
        self._recover_from_snapshot()

        while self._running:
            # Start new window (unless we resumed one from crash recovery)
            if self.window is None or self.window.is_expired:
                self.window = WindowState(
                    started_at=datetime.now(timezone.utc),
                    cycle_time_minutes=self.settings.cycle_time_minutes,
                )
                logger.info("New cycle window started, deadline: %s", self.window.deadline.isoformat())

                # Re-score priorities at the start of each new window
                await self._rescore_priorities()

            # Run window (role health expected/actual updated inside)
            await self._run_window()

            # Snapshot after window
            self._snapshot()

            # If window ended early (all work done), wait for next window
            if not self.window.is_expired:
                wait = self.window.remaining_seconds
                logger.info("Window work complete, idling %.0fs until next window", wait)
                await asyncio.sleep(wait)

    async def _run_window(self) -> None:
        """Execute work within the current cycle window.

        Uses asyncio tasks so workers run concurrently and the scheduler
        re-evaluates as soon as any worker finishes (not batch-and-wait).
        """
        pending_tasks: dict[asyncio.Task, Worker] = {}

        while not self.window.is_expired and self._running:
            # Check for completed tasks
            done = {t for t in pending_tasks if t.done()}
            for task in done:
                worker = pending_tasks.pop(task)
                try:
                    result = task.result()
                    if isinstance(result, RunResult):
                        await self._handle_result(result)
                except Exception as e:
                    logger.error("Worker task failed: %s", e)

            ideas = self._get_active_ideas()
            if not ideas:
                if not pending_tasks:
                    logger.debug("No active ideas and no pending work, waiting")
                    await asyncio.sleep(5)
                    continue
                # Wait for a pending task to complete
                await asyncio.sleep(1)
                continue

            # Find available workers
            idle_workers = [w for w in self.workers if w.is_idle]
            if not idle_workers:
                # All workers busy, wait for one to finish
                if pending_tasks:
                    done_set, _ = await asyncio.wait(
                        pending_tasks.keys(), return_when=asyncio.FIRST_COMPLETED,
                        timeout=30.0,
                    )
                    self._snapshot()  # Periodic state update for dashboard
                    # Results will be processed at top of loop
                else:
                    await asyncio.sleep(1)
                continue

            # Build work queue
            locked = {w.current_idea for w in self.workers if not w.is_idle}
            queue = self._build_work_queue(ideas, self.window.serviced, locked)

            if not queue:
                if not pending_tasks:
                    logger.info("All eligible work done for this window")
                    break
                # Wait for remaining tasks
                await asyncio.sleep(1)
                continue

            # Dispatch work to idle workers as async tasks
            for worker, (role, idea_id) in zip(idle_workers, queue):
                # Track that this role had eligible work this window
                self.role_health[role].expected += 1
                self.window.mark_serviced(role, idea_id)
                task = asyncio.create_task(
                    self._run_worker(worker, role, idea_id),
                    name=f"worker-{worker.worker_id}-{role}-{idea_id}",
                )
                pending_tasks[task] = worker

            # Yield to event loop so tasks can start and update worker state
            await asyncio.sleep(0)
            self._snapshot()

        # Wait for any remaining tasks at window end
        if pending_tasks:
            logger.info("Window ending, waiting for %d pending tasks", len(pending_tasks))
            results = await asyncio.gather(*pending_tasks.keys(), return_exceptions=True)
            for r in results:
                if isinstance(r, RunResult):
                    await self._handle_result(r)
                elif isinstance(r, Exception):
                    logger.error("Worker task failed at window end: %s", r)

    async def _run_worker(self, worker: Worker, role: str, idea_id: str) -> RunResult | None:
        """Run a single worker assignment."""
        return await worker.execute(role, idea_id, self.window.deadline)

    def stop(self) -> None:
        """Signal the pool to stop after current work completes."""
        self._running = False
        logger.info("Pool stop requested")
