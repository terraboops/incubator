"""PoolManager: continuous priority-queue scheduler for agent work.

Design verified by TLA+ model checking (specs/pool_scheduler.tla).
The pool is the single executor — no competing orchestrator pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from trellis.config import Settings
from trellis.core.blackboard import Blackboard
from trellis.core.lock import LockManager
from trellis.core.registry import load_registry
from trellis.orchestrator.job_queue import (
    Job,
    JobQueue,
    CadenceTracker,
    compute_priority,
    PRIORITY_DEFAULT,
    PRIORITY_EARLY_BOOST,
)
from trellis.orchestrator.worker import Worker, RunResult, RunStatus

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 10
MAX_ITERATE_PER_STAGE = 3
POOL_LOCK_FILE = ".pool.lock"
POOL_RESTART_DELAY_SECONDS = 5
MAX_RAPID_RESTARTS = 5
RAPID_RESTART_WINDOW_SECONDS = 60


def can_schedule(
    role: str,
    idea_id: str,
    running_jobs: set[tuple[str, str]],
    pipeline: dict,
) -> bool:
    """Check if (role, idea_id) can run given what's already running.

    Agents in the same parallel_group are serialized on the same idea.
    Agents in different groups can overlap.
    """
    if idea_id == "__all__":
        return True

    groups = pipeline.get("parallel_groups", [pipeline.get("agents", pipeline.get("stages", []))])

    # Find which group this role belongs to
    my_group = None
    for group in groups:
        if role in group:
            my_group = group
            break

    if my_group is None:
        return True  # not in any group = can always run

    # Check: is any agent from MY group already running on this idea?
    for running_role, running_idea in running_jobs:
        if running_idea == idea_id and running_role in my_group and running_role != role:
            return False

    return True


class PoolManager:
    """Manages the worker pool with priority-queue scheduling."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.blackboard = Blackboard(settings.blackboard_dir)
        self.lock_manager = LockManager()
        self.registry = load_registry(settings.registry_path)

        # Derive roles from registry
        self.roles = [a.name for a in self.registry.agents.values() if a.status == "active"]

        # State
        self.workers: list[Worker] = []
        self._running = False
        self._job_kinds: dict[tuple[str, str], str] = {}  # (role, idea_id) -> job kind

        # Pool dir for snapshots
        self.pool_dir = settings.project_root / "pool"
        self.pool_dir.mkdir(exist_ok=True)

    def _get_active_ideas(self) -> list[dict]:
        """Get ideas that need pipeline work, sorted by priority.

        Excludes killed, paused, review-pending, and fully-released ideas.
        """
        ideas = []
        for idea_id in self.blackboard.list_ideas():
            status = self.blackboard.get_status(idea_id)
            phase = status.get("phase", "submitted")
            if phase in ("killed", "paused"):
                continue
            if phase == "released" and not self.blackboard.pending_post_ready(idea_id):
                continue
            if phase.endswith("_review"):
                continue
            if status.get("needs_human_review") and not self.settings.telegram_bot_token:
                continue
            score = status.get("priority_score", PRIORITY_DEFAULT)
            if phase in ("submitted",) or self._is_first_pipeline_agent(idea_id):
                score += PRIORITY_EARLY_BOOST
            status["_effective_priority"] = score
            ideas.append(status)
        ideas.sort(key=lambda s: s.get("_effective_priority", 0), reverse=True)
        return ideas

    def _get_all_ideas(self) -> list[dict]:
        """Get ALL ideas (except killed) for background/watcher agents.

        Watchers and global agents always run on every living idea —
        there's always new research, competitive intel, or refinements.
        """
        ideas = []
        for idea_id in self.blackboard.list_ideas():
            status = self.blackboard.get_status(idea_id)
            if status.get("phase") == "killed":
                continue
            score = status.get("priority_score", PRIORITY_DEFAULT)
            status["_effective_priority"] = score
            ideas.append(status)
        ideas.sort(key=lambda s: s.get("_effective_priority", 0), reverse=True)
        return ideas

    def _is_first_pipeline_agent(self, idea_id: str) -> bool:
        """Check if idea is at its first pipeline agent."""
        pipeline = self.blackboard.get_pipeline(idea_id)
        agents = pipeline.get("agents", pipeline.get("stages", []))
        if not agents:
            return False
        next_agent = self.blackboard.next_agent(idea_id)
        return next_agent == agents[0]

    def _max_concurrent(self, role: str) -> int:
        """Get the max concurrent instances for a role from registry."""
        agent_config = self.registry.get_agent(role)
        return agent_config.max_concurrent if agent_config else 1

    # ── Producers ────────────────────────────────────────────────────

    def _pipeline_producer(self, queue: JobQueue) -> None:
        """Scan ideas and enqueue next pipeline agents."""
        ideas = self._get_active_ideas()
        for idea in ideas:
            idea_id = idea["id"]
            priority = idea.get("_effective_priority", PRIORITY_DEFAULT)

            if self.blackboard.is_ready(idea_id):
                # Main agents done — enqueue post_ready work
                for post_role in self.blackboard.pending_post_ready(idea_id):
                    if post_role not in self.roles:
                        continue
                    job = Job(
                        priority=compute_priority("pipeline", priority, is_first_agent=False),
                        kind="pipeline",
                        role=post_role,
                        idea_id=idea_id,
                    )
                    queue.enqueue(job)
                continue

            next_role = self.blackboard.next_agent(idea_id)
            if not next_role:
                continue

            is_first = self._is_first_pipeline_agent(idea_id)
            job = Job(
                priority=compute_priority("pipeline", priority, is_first_agent=is_first),
                kind="pipeline",
                role=next_role,
                idea_id=idea_id,
            )
            queue.enqueue(job)

        # Global agents (phase="*") — one job per active idea
        # Only enqueued by pipeline_producer if they have no cadence;
        # cadence-tracked global agents are handled by _cadence_producer.
        for role in self.roles:
            config = self.registry.get_agent(role)
            if not config or config.phase != "*":
                continue
            if config.status != "active":
                continue
            if config.cadence:
                continue  # cadence_producer handles these
            for idea in ideas:
                job = Job(
                    priority=PRIORITY_DEFAULT,
                    kind="pipeline",
                    role=role,
                    idea_id=idea["id"],
                )
                queue.enqueue(job)

    def _feedback_producer(self, queue: JobQueue) -> None:
        """Scan ALL non-killed ideas for unacknowledged feedback and enqueue jobs.

        Runs independently of _pipeline_producer so feedback is serviced even
        when an idea is in a review phase, fully released, or has no next
        pipeline agent.
        """
        for idea_id in self.blackboard.list_ideas():
            status = self.blackboard.get_status(idea_id)
            phase = status.get("phase", "submitted")
            if phase == "killed":
                continue
            priority = status.get("priority_score", PRIORITY_DEFAULT)
            for role in self.roles:
                config = self.registry.get_agent(role)
                if config and (config.cadence or config.phase == "*"):
                    continue
                if self.blackboard.has_pending_feedback(idea_id, role):
                    job = Job(
                        priority=compute_priority("feedback", priority),
                        kind="feedback",
                        role=role,
                        idea_id=idea_id,
                    )
                    queue.enqueue(job)

    def _cadence_producer(
        self, queue: JobQueue, cadence_trackers: dict[str, CadenceTracker]
    ) -> None:
        """Enqueue background jobs when their cadence is due.

        Background/watcher agents run on ALL ideas (except killed) —
        there's always new research, competitive intel, or refinements.
        """
        now = datetime.now(timezone.utc)
        for role, tracker in cadence_trackers.items():
            if not tracker.is_due(now):
                continue
            priority = tracker.priority(now)
            if self._is_global_agent(role):
                # Global agents (phase="*") run per-idea
                ideas = self._get_all_ideas()
                for idea in ideas:
                    job = Job(
                        priority=priority,
                        kind="background",
                        role=role,
                        idea_id=idea["id"],
                    )
                    queue.enqueue(job)
            else:
                # All background/watcher agents run per-idea
                ideas = self._get_all_ideas()
                for idea in ideas:
                    job = Job(
                        priority=priority,
                        kind="background",
                        role=role,
                        idea_id=idea["id"],
                    )
                    queue.enqueue(job)

    # ── Dispatch ─────────────────────────────────────────────────────

    def _pop_schedulable(self, queue: JobQueue, running: set[tuple[str, str]]) -> Job | None:
        """Pop highest-priority job that passes scheduling constraints.

        TLA+ verified: MUST return highest-priority schedulable job.
        Skipped jobs are re-enqueued to preserve them for later.
        """
        skipped: list[Job] = []
        result = None

        while True:
            job = queue.pop()
            if job is None:
                break

            # Check max_concurrent for this role across the pool
            role_count = sum(1 for r, _ in running if r == job.role)
            if role_count >= self._max_concurrent(job.role):
                skipped.append(job)
                continue

            # Check parallel group constraints for same-idea scheduling
            pipeline = self.blackboard.get_pipeline(job.idea_id)
            if not can_schedule(job.role, job.idea_id, running, pipeline):
                skipped.append(job)
                continue

            result = job
            break

        # Re-enqueue skipped jobs
        for job in skipped:
            queue.mark_done(job.role, job.idea_id)
            queue.enqueue(job)

        return result

    # ── Result handling ──────────────────────────────────────────────

    def _is_global_agent(self, role: str) -> bool:
        """Check if a role is a global (phase='*') agent."""
        config = self.registry.get_agent(role)
        return config is not None and config.phase == "*"

    def _is_background_agent(self, role: str) -> bool:
        """Check if a role is a background/watcher agent (has cadence or phase='*')."""
        config = self.registry.get_agent(role)
        if config is None:
            return False
        return config.cadence is not None or config.phase == "*"

    async def _handle_result(self, result: RunResult, queue: JobQueue) -> None:
        """Process a completed worker run — update tracking, apply gating."""
        # Background/watcher agents don't affect pipeline state — but if
        # they're in post_ready, we must mark them serviced so the idea
        # doesn't stay perpetually "active" in the scheduler.
        if self._is_background_agent(result.role):
            if result.status in (RunStatus.OK, RunStatus.DEADLINE):
                if self.blackboard.pipeline_has_role(result.idea_id, result.role):
                    status = self.blackboard.get_status(result.idea_id)
                    serviced = status.get("last_serviced_by", {})
                    serviced[result.role] = datetime.now(timezone.utc).isoformat()
                    self.blackboard.update_status(
                        result.idea_id,
                        last_serviced_by=serviced,
                    )
            return

        now = datetime.now(timezone.utc)

        if result.status == RunStatus.ERROR:
            self._job_kinds.pop((result.role, result.idea_id), None)
            self.blackboard.update_status(
                result.idea_id,
                last_error=result.error or "Unknown error",
                last_error_agent=result.role,
                last_error_at=now.isoformat(),
            )
            self._broadcast_sync(
                "activity",
                {
                    "idea_id": result.idea_id,
                    "message": f"{result.role} failed: {result.error}",
                    "kind": "error",
                },
            )
            # mark_done called by caller — pipeline_producer will re-enqueue on next scan
            return

        if result.status == RunStatus.DEADLINE:
            status = self.blackboard.get_status(result.idea_id)
            hits = status.get("deadline_hits", {})
            hits[result.role] = hits.get(result.role, 0) + 1
            self.blackboard.update_status(result.idea_id, deadline_hits=hits)

        if result.status in (RunStatus.OK, RunStatus.DEADLINE):
            # Read agent's recommendation
            status = self.blackboard.get_status(result.idea_id)
            recommendation = status.get("phase_recommendation", "proceed")

            # Determine job kind — feedback runs don't increment iter counts
            job_kind = self._job_kinds.pop((result.role, result.idea_id), "pipeline")
            is_feedback = job_kind == "feedback"

            # Per-agent iteration tracking (matches TLA+ iterCount[idea][agent])
            iter_counts = status.get("iter_counts", {})
            if not is_feedback:
                iter_counts[result.role] = iter_counts.get(result.role, 0) + 1

            # Update tracking
            serviced = status.get("last_serviced_by", {})
            serviced[result.role] = now.isoformat()
            stage_results = status.get("stage_results", {})
            stage_results[result.role] = recommendation

            # Session tracking
            session_ids = status.get("session_ids", [])
            if result.session_id:
                session_ids.append(
                    {
                        "agent": result.role,
                        "session_id": result.session_id,
                        "at": now.isoformat(),
                    }
                )

            # Sandbox failure tracking
            sandbox_failure_count = status.get("sandbox_failure_count", 0)
            if result.sandbox_failure:
                sandbox_failure_count += 1

            self.blackboard.update_status(
                result.idea_id,
                last_serviced_by=serviced,
                stage_results=stage_results,
                total_cost_usd=status.get("total_cost_usd", 0) + result.cost_usd,
                iter_counts=iter_counts,
                iteration_count=sum(iter_counts.values()),  # backward compat
                session_ids=session_ids,
                sandbox_failure_count=sandbox_failure_count,
            )

            # Apply gating inline
            gating_mode = self.blackboard.get_gating_mode(result.idea_id, result.role)

            if gating_mode == "human-review":
                self.blackboard.update_status(
                    result.idea_id,
                    needs_human_review=True,
                    review_reason=f"Human review required after {result.role}",
                )
                return
            if gating_mode == "llm-decides" and recommendation == "needs_review":
                self.blackboard.update_status(
                    result.idea_id,
                    needs_human_review=True,
                    review_reason=status.get("phase_reasoning", "Agent flagged uncertainty"),
                )
                return
            if gating_mode == "auto" and recommendation == "iterate" and not is_feedback:
                agent_iters = iter_counts.get(result.role, 0)
                max_iterate = (
                    self.settings.max_iterate_per_stage
                    if hasattr(self.settings, "max_iterate_per_stage")
                    else MAX_ITERATE_PER_STAGE
                )
                if agent_iters >= max_iterate:
                    self.blackboard.update_status(
                        result.idea_id,
                        needs_human_review=True,
                        review_reason=f"{result.role} hit max iterations ({max_iterate})",
                    )
                    return
                # pipeline_producer will see "iterate" and re-enqueue
                return
            if recommendation == "kill":
                self.blackboard.update_status(result.idea_id, phase="killed")
                return

            # "proceed" — advance phase and check completion
            # TLA+ verified: release MUST be atomic with last completion
            # Reset proceeding agent's iter count (matches TLA+ iterCount'[idea][agent] = 0)
            iter_counts = status.get("iter_counts", {})
            iter_counts[result.role] = 0
            self.blackboard.update_status(
                result.idea_id, iter_counts=iter_counts, iteration_count=sum(iter_counts.values())
            )

            next_agent = self.blackboard.next_agent(result.idea_id)
            if next_agent:
                old_phase = status.get("phase", "submitted")
                if old_phase != next_agent:
                    history = status.get("phase_history", [])
                    history.append(
                        {
                            "from": old_phase,
                            "to": next_agent,
                            "at": now.isoformat(),
                        }
                    )
                    self.blackboard.update_status(
                        result.idea_id,
                        phase=next_agent,
                        phase_history=history,
                    )
                    logger.info(
                        "Idea '%s' advanced: %s -> %s", result.idea_id, old_phase, next_agent
                    )
            elif self.blackboard.is_ready(result.idea_id):
                self._handle_release(result.idea_id)

        # Broadcast completion
        self._broadcast_sync(
            "worker_done",
            {
                "worker_id": result.role,
                "idea_id": result.idea_id,
                "status": result.status.value,
                "duration": result.duration_seconds,
            },
        )

    def _handle_release(self, idea_id: str) -> None:
        """Release an idea atomically after last pipeline agent completes.

        Enforces quality gate: if priority_score < min_quality_score,
        the idea loops back for refinement instead of releasing.
        """
        status = self.blackboard.get_status(idea_id)
        now = datetime.now(timezone.utc)
        old_phase = status.get("phase", "submitted")
        history = status.get("phase_history", [])
        prior_releases = sum(1 for e in history if e.get("to") == "released")
        history.append(
            {
                "from": old_phase,
                "to": "released",
                "at": now.isoformat(),
            }
        )
        max_refinement_cycles = status.get(
            "max_refinement_cycles",
            self.settings.max_refinement_cycles,
        )
        at_cycle_cap = max_refinement_cycles > 0 and prior_releases >= max_refinement_cycles

        # Quality gate check
        min_score = self.settings.min_quality_score
        score = status.get("priority_score", 0)
        quality_ok = min_score <= 0 or score >= min_score

        if not quality_ok and at_cycle_cap:
            # Quality still low after all refinement cycles — needs human decision
            self.blackboard.update_status(
                idea_id,
                needs_human_review=True,
                phase_history=history,
                review_reason=f"Quality score {score:.1f} below threshold {min_score:.1f} after {prior_releases} refinement cycles",
            )
            logger.info(
                "Idea '%s' failed quality gate (%.1f < %.1f) at cycle cap — human review",
                idea_id,
                score,
                min_score,
            )
            return

        if not quality_ok or not at_cycle_cap:
            # Loop back for refinement (quality-driven or normal refinement)
            if (
                max_refinement_cycles == 0
                or prior_releases < max_refinement_cycles
                or not quality_ok
            ):
                serviced = status.get("last_serviced_by", {})
                pipeline = self.blackboard.get_pipeline(idea_id)
                for role in pipeline.get("post_ready", []):
                    serviced.pop(role, None)
                reason = "quality gate" if not quality_ok else "refinement"
                self.blackboard.update_status(
                    idea_id,
                    phase="submitted",
                    phase_history=history,
                    stage_results={},
                    last_serviced_by=serviced,
                )
                logger.info(
                    "Idea '%s' looping for %s (release %d/%s, score %.1f)",
                    idea_id,
                    reason,
                    prior_releases + 1,
                    max_refinement_cycles or "∞",
                    score,
                )
                return

        # Terminal "released" — quality is OK and at cycle cap
        self.blackboard.update_status(
            idea_id,
            phase="released",
            phase_history=history,
        )
        logger.info(
            "Idea '%s' reached max refinement cycles (%d), marking terminal",
            idea_id,
            max_refinement_cycles,
        )

    @staticmethod
    def _broadcast_sync(event_type: str, data: dict) -> None:
        """Best-effort broadcast — fire and forget."""
        try:
            from trellis.web.api.websocket import broadcast_event

            asyncio.get_event_loop().create_task(broadcast_event(event_type, data))
        except Exception:
            pass

    # ── Process locking ──────────────────────────────────────────────

    def _acquire_pool_lock(self) -> bool:
        """PID-file lock. Returns False if another pool is running."""
        lock_path = self.settings.project_root / POOL_LOCK_FILE
        if lock_path.exists():
            try:
                pid = int(lock_path.read_text().strip())
                os.kill(pid, 0)  # check if process exists
                logger.error(
                    "Another pool is running (PID %d). Lock file: %s\n"
                    "If this is stale, remove it: rm %s",
                    pid,
                    lock_path,
                    lock_path,
                )
                return False
            except ProcessLookupError:
                logger.warning(
                    "Stale pool lock found (PID %d is dead). Cleaning up: %s",
                    pid,
                    lock_path,
                )
                lock_path.unlink(missing_ok=True)
            except (OSError, ValueError) as e:
                logger.warning("Bad pool lock file (%s), removing: %s", e, lock_path)
                lock_path.unlink(missing_ok=True)
        lock_path.write_text(str(os.getpid()))
        logger.info("Pool lock acquired (PID %d): %s", os.getpid(), lock_path)
        return True

    def _release_pool_lock(self) -> None:
        """Release the PID-file lock."""
        lock_path = self.settings.project_root / POOL_LOCK_FILE
        if lock_path.exists():
            try:
                pid = int(lock_path.read_text().strip())
                if pid == os.getpid():
                    lock_path.unlink(missing_ok=True)
                    logger.info("Pool lock released: %s", lock_path)
                else:
                    logger.warning(
                        "Pool lock held by different PID %d (we are %d), not releasing: %s",
                        pid,
                        os.getpid(),
                        lock_path,
                    )
            except (OSError, ValueError):
                lock_path.unlink(missing_ok=True)
                logger.info("Cleaned up bad pool lock: %s", lock_path)

    # ── Snapshot ─────────────────────────────────────────────────────

    def _snapshot(
        self,
        queue: JobQueue | None = None,
        cadence_trackers: dict[str, CadenceTracker] | None = None,
    ) -> None:
        """Write pool state to filesystem for web UI."""
        worker_data = []
        for w in self.workers:
            if w.is_idle:
                worker_data.append({"id": w.worker_id, "status": "idle"})
            else:
                elapsed = (
                    (datetime.now(timezone.utc) - w.started_at).total_seconds()
                    if w.started_at
                    else 0
                )
                worker_data.append(
                    {
                        "id": w.worker_id,
                        "status": "active",
                        "role": w.current_role,
                        "idea": w.current_idea,
                        "started_at": w.started_at.isoformat() if w.started_at else None,
                        "elapsed_seconds": elapsed,
                    }
                )

        cadence_data = {}
        if cadence_trackers:
            now = datetime.now(timezone.utc)
            for role, tracker in cadence_trackers.items():
                cadence_data[role] = {
                    "cron": tracker.cron_expr,
                    "last_run_at": tracker.last_run_at.isoformat() if tracker.last_run_at else None,
                    "elapsed_ratio": round(tracker.elapsed_ratio(now), 2),
                    "is_due": tracker.is_due(now),
                    "priority": round(tracker.priority(now), 1),
                }

        state = {
            "pool_size": self.settings.pool_size,
            "queue_depth": queue.depth if queue else 0,
            "workers": worker_data,
            "cadence_trackers": cadence_data,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        state_path = self.pool_dir / "state.json"
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.pool_dir, suffix=".tmp")
        try:
            with open(tmp_fd, "w") as f:
                json.dump(state, f, indent=2)
            Path(tmp_path).replace(state_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    # ── Main loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main pool loop. Runs until stopped."""
        self._running = True

        if not self._acquire_pool_lock():
            logger.error("Another pool is already running. Exiting.")
            return

        try:
            logger.info("Pool starting with %d workers", self.settings.pool_size)
            await self._run_loop()
        finally:
            self._release_pool_lock()

    async def _run_loop(self) -> None:
        """Core scheduling loop."""
        # Create workers
        from trellis.core.agent_factory import AgentFactory
        from trellis.comms.notifications import NotificationDispatcher
        from trellis.comms.telegram import TelegramNotifier

        telegram = TelegramNotifier(
            self.settings.telegram_bot_token, self.settings.telegram_chat_id
        )
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

        # Build cadence trackers from registry
        cadence_trackers: dict[str, CadenceTracker] = {}
        for agent in self.registry.agents.values():
            if agent.cadence and agent.status == "active":
                cadence_trackers[agent.name] = CadenceTracker(agent.name, agent.cadence)

        queue = JobQueue()
        pending: dict[asyncio.Task, Worker] = {}
        now = datetime.now(timezone.utc)

        # Initial priority scoring
        await self._rescore_priorities()

        while self._running:
            now = datetime.now(timezone.utc)

            # 1. Reap completed tasks
            for task in [t for t in pending if t.done()]:
                worker = pending.pop(task)
                try:
                    result = task.result()
                    if isinstance(result, RunResult):
                        try:
                            await self._handle_result(result, queue)
                        except OSError as e:
                            logger.error(
                                "I/O error handling result for %s on %s (non-fatal): %s",
                                result.role,
                                result.idea_id,
                                e,
                            )
                        queue.mark_done(result.role, result.idea_id)
                        # Reset cadence on ANY completion (TLA+ verified)
                        if result.role in cadence_trackers:
                            cadence_trackers[result.role].last_run_at = now
                except Exception as e:
                    logger.error("Worker task failed: %s", e)

            # 2. Pipeline producer — scan ideas, enqueue next agents
            self._pipeline_producer(queue)

            # 3. Feedback producer — independent of pipeline state
            self._feedback_producer(queue)

            # 4. Cadence producer — enqueue background jobs when due
            self._cadence_producer(queue, cadence_trackers)

            # 5. Dispatch to idle workers (highest priority first)
            running = {(w.current_role, w.current_idea) for w in self.workers if not w.is_idle}
            for worker in self.workers:
                if not worker.is_idle:
                    continue
                job = self._pop_schedulable(queue, running)
                if job is None:
                    continue
                self._job_kinds[(job.role, job.idea_id)] = job.kind
                task = asyncio.create_task(
                    self._run_worker(worker, job),
                    name=f"worker-{worker.worker_id}-{job.role}-{job.idea_id}",
                )
                pending[task] = worker
                running.add((job.role, job.idea_id))

            # 6. Wait for something to happen
            if pending:
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED, timeout=10)
            else:
                await asyncio.sleep(self.settings.producer_interval_seconds)

            try:
                self._snapshot(queue, cadence_trackers)
            except OSError as e:
                logger.error("Snapshot failed (non-fatal): %s", e)

    async def _run_worker(self, worker: Worker, job: Job) -> RunResult | None:
        """Run a single worker assignment."""
        from trellis.otel import get_tracer

        tracer = get_tracer()
        timeout = self.settings.job_timeout_minutes * 60
        with tracer.start_as_current_span("agent.run") as span:
            span.set_attribute("agent.name", job.role)
            span.set_attribute("idea.id", job.idea_id)
            span.set_attribute("job.kind", job.kind)
            span.set_attribute("worker.id", worker.worker_id)
            result = await worker.execute(job, timeout)
            if result:
                span.set_attribute("cost.usd", result.cost_usd)
                span.set_attribute("duration.seconds", result.duration_seconds)
                span.set_attribute("status", result.status.value)
                span.set_attribute("sandbox.failure", result.sandbox_failure)
                if result.session_id:
                    span.set_attribute("session.id", result.session_id)
                if result.sandbox_failure:
                    span.add_event("sandbox.denied", {"stderr": result.error or ""})
                if result.error:
                    span.record_exception(RuntimeError(result.error))
            return result

    async def _rescore_priorities(self) -> None:
        """Re-score priorities for all active ideas."""
        try:
            from trellis.orchestrator.orchestrator import Orchestrator

            orchestrator = Orchestrator.__new__(Orchestrator)
            orchestrator.blackboard = self.blackboard
            orchestrator.settings = self.settings
            await orchestrator.score_priorities()
            logger.info("Priority scores updated")
        except Exception as e:
            logger.warning("Priority re-scoring failed: %s", e)

    def stop(self) -> None:
        """Signal the pool to stop after current work completes."""
        self._running = False
        logger.info("Pool stop requested")
