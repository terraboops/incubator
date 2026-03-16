"""Core orchestrator: phase transitions, agent dispatch, human approvals."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from incubator.comms.notifications import NotificationDispatcher
from incubator.config import Settings
from incubator.core.agent_factory import AgentFactory
from incubator.core.blackboard import Blackboard
from incubator.core.lock import LockManager
from incubator.core.phase import (
    Phase,
    REVIEW_TO_AGENT_PHASE,
    REVIEW_TO_NEXT_PHASE,
    can_transition,
)
from incubator.core.registry import load_registry

logger = logging.getLogger(__name__)

PHASE_TO_AGENT = {
    Phase.IDEATION: "ideation",
    Phase.IMPLEMENTATION: "implementation",
    Phase.VALIDATION: "validation",
    Phase.RELEASE: "release",
}

MAX_ITERATIONS = 3
MAX_ACTIVE_IDEAS = 5
PRIORITY_SCORE_INTERVAL = 5  # Re-score every N continuous loop iterations
CONTINUOUS_SLEEP_SECONDS = 10


async def _broadcast(event_type: str, **kwargs):
    """Best-effort broadcast to WebSocket clients."""
    try:
        from incubator.web.api.websocket import broadcast_event
        await broadcast_event(event_type, kwargs)
    except Exception:
        pass  # Web server may not be running


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.blackboard = Blackboard(settings.blackboard_dir)
        self.lock_manager = LockManager()
        self.registry = load_registry(settings.registry_path)

        from incubator.comms.telegram import TelegramNotifier

        telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self.dispatcher = NotificationDispatcher(telegram)
        self.factory = AgentFactory(
            registry=self.registry,
            blackboard=self.blackboard,
            dispatcher=self.dispatcher,
            project_root=settings.project_root,
        )

    async def incubate(self, title: str, description: str) -> str:
        """Create a new idea and set up its pipeline. Pool handles execution."""
        idea_id = self.blackboard.create_idea(title, description)

        # Set default pipeline config
        from incubator.core.blackboard import DEFAULT_PIPELINE
        import json
        pipeline = json.loads(json.dumps(DEFAULT_PIPELINE))
        self.blackboard.set_pipeline(idea_id, pipeline)

        await self.dispatcher.notify(f"🆕 New idea: *{idea_id}*\n\n{description[:200]}")
        await _broadcast("idea_created", idea_id=idea_id, title=title)
        return idea_id

    async def run_continuous_for_idea(self, idea_id: str) -> None:
        """Run an idea through continuous refinement cycles until stop is requested.

        After each pipeline run completes, checks `stop_requested` in status.json.
        If not set, transitions back to ideation and runs again.
        """
        self.blackboard.update_status(idea_id, running=True, stop_requested=False)
        try:
            while True:
                await self._run_pipeline(idea_id)

                # Check stop flag
                status = self.blackboard.get_status(idea_id)
                if status.get("stop_requested"):
                    logger.info("Stop requested for %s, ending continuous run", idea_id)
                    break

                phase = Phase(status["phase"])
                if phase in (Phase.KILLED, Phase.PAUSED):
                    logger.info("%s reached %s, ending continuous run", idea_id, phase.value)
                    break

                if phase == Phase.RELEASED:
                    history = status.get("phase_history", [])
                    prior_releases = sum(1 for e in history if e.get("to") == "released")
                    max_refinement_cycles = status.get("max_refinement_cycles", 1)
                    if max_refinement_cycles != 0 and prior_releases >= max_refinement_cycles:
                        logger.info(
                            "%s reached max refinement cycles (%d), ending continuous run",
                            idea_id, max_refinement_cycles,
                        )
                        break
                    logger.info(
                        "Starting refinement cycle for %s (release %d/%d)",
                        idea_id, prior_releases, max_refinement_cycles if max_refinement_cycles != 0 else "unlimited",
                    )
                    await self._transition(idea_id, Phase.IDEATION)
                else:
                    # Pipeline stopped mid-way (e.g. review pending), don't auto-loop
                    logger.info("%s at %s, pausing continuous run", idea_id, phase.value)
                    break
        finally:
            self.blackboard.update_status(idea_id, running=False, stop_requested=False)

    def request_stop(self, idea_id: str) -> None:
        """Signal the continuous loop to stop after the current cycle completes."""
        self.blackboard.update_status(idea_id, stop_requested=True)

    async def _run_pipeline(self, idea_id: str) -> None:
        """Run an idea through phases until released, killed, or paused.

        After release, the continuous loop will pick it back up for refinement.
        A single pipeline run goes: ideation → implementation → validation → release → released (stop).
        """
        status = self.blackboard.get_status(idea_id)
        current_phase = Phase(status["phase"])

        if current_phase == Phase.SUBMITTED:
            await self._transition(idea_id, Phase.IDEATION)
            current_phase = Phase.IDEATION

        while current_phase not in (Phase.RELEASED, Phase.KILLED, Phase.PAUSED):
            agent_name = PHASE_TO_AGENT.get(current_phase)
            if not agent_name:
                break

            next_phase = await self._run_phase(idea_id, current_phase, agent_name)
            if next_phase:
                current_phase = next_phase
            else:
                break

    async def _run_phase(self, idea_id: str, phase: Phase, agent_name: str) -> Phase | None:
        """Run a single agent phase and handle the result."""
        if not self.lock_manager.acquire("phase", idea_id, executor=agent_name):
            logger.warning("Could not acquire lock for %s/%s", idea_id, agent_name)
            await self.dispatcher.notify_error(idea_id, f"Lock held, skipping {agent_name}")
            return None

        try:
            # Broadcast agent starting
            await _broadcast(
                "agent_status",
                idea_id=idea_id,
                agent=agent_name,
                status="running",
                detail=f"Starting {agent_name} phase",
            )
            await _broadcast(
                "activity",
                idea_id=idea_id,
                message=f"{agent_name} agent is now working",
                kind="agent_start",
            )

            agent = self.factory.create_agent(agent_name)
            result = await agent.run(idea_id)

            # Broadcast agent finished
            await _broadcast(
                "agent_status",
                idea_id=idea_id,
                agent=agent_name,
                status="done" if result.success else "error",
                detail=result.error or "Completed",
            )

            # Update cost tracking
            status = self.blackboard.get_status(idea_id)
            total_cost = status.get("total_cost_usd", 0.0) + result.cost_usd
            iteration = status.get("iteration_count", 0) + 1
            self.blackboard.update_status(
                idea_id, total_cost_usd=total_cost, iteration_count=iteration
            )

            if not result.success:
                error_msg = result.error or "Agent failed without error message"
                self.blackboard.update_status(
                    idea_id,
                    last_error=error_msg,
                    last_error_agent=agent_name,
                    last_error_at=datetime.now(timezone.utc).isoformat(),
                )
                await self.dispatcher.notify_error(idea_id, error_msg)
                await _broadcast(
                    "activity",
                    idea_id=idea_id,
                    message=f"{agent_name} agent failed: {error_msg}",
                    kind="error",
                )
                return None

            # Clear any previous error state on success
            self.blackboard.update_status(
                idea_id, last_error=None, last_error_agent=None, last_error_at=None
            )
            await _broadcast(
                "activity",
                idea_id=idea_id,
                message=f"{agent_name} agent finished (${result.cost_usd:.2f})",
                kind="agent_done",
            )

            # Read the recommendation
            status = self.blackboard.get_status(idea_id)
            recommendation = status.get("phase_recommendation", "proceed")
            reasoning = status.get("phase_reasoning", "")

            review_phases = {
                Phase.IDEATION: Phase.IDEATION_REVIEW,
                Phase.IMPLEMENTATION: Phase.IMPLEMENTATION_REVIEW,
                Phase.VALIDATION: Phase.VALIDATION_REVIEW,
                Phase.RELEASE: Phase.RELEASED,
            }
            review_phase = review_phases.get(phase)
            if not review_phase:
                return None

            if phase == Phase.RELEASE:
                await self._transition(idea_id, Phase.RELEASED)
                release_count = self._count_releases(idea_id)
                if release_count <= 1:
                    await self.dispatcher.notify(f"[Released] *{idea_id}* has been released!")
                else:
                    await self.dispatcher.notify(
                        f"[Cycle] *{idea_id}* refinement cycle #{release_count} complete"
                    )
                return Phase.RELEASED

            # Move to review phase
            await self._transition(idea_id, review_phase)

            # Ask human for approval
            await _broadcast(
                "activity",
                idea_id=idea_id,
                message=f"Waiting for your review (recommends: {recommendation})",
                kind="waiting",
            )

            return await self._human_review(
                idea_id, review_phase, recommendation, reasoning, iteration
            )
        finally:
            self.lock_manager.release("phase", idea_id)

    def _count_releases(self, idea_id: str) -> int:
        """Count how many times an idea has reached the released phase."""
        status = self.blackboard.get_status(idea_id)
        history = status.get("phase_history", [])
        return sum(1 for entry in history if entry.get("to") == "released")

    async def _human_review(
        self,
        idea_id: str,
        review_phase: Phase,
        recommendation: str,
        reasoning: str,
        iteration: int,
    ) -> Phase | None:
        """Ask human to approve/reject/kill at a review phase."""
        next_phase = REVIEW_TO_NEXT_PHASE.get(review_phase)
        agent_phase = REVIEW_TO_AGENT_PHASE.get(review_phase)

        question = (
            f"*{idea_id}* — Review ({review_phase.value})\n\n"
            f"Agent recommends: `{recommendation}`\n"
            f"{reasoning}\n\n"
            f"Iteration: {iteration}/{MAX_ITERATIONS}\n"
            f"What would you like to do?"
        )

        options = ["approve", "iterate", "kill", "pause"]
        if iteration >= MAX_ITERATIONS:
            question += f"\n⚠️ Max iterations ({MAX_ITERATIONS}) reached."

        response = await self.dispatcher.ask(question, options)

        await _broadcast(
            "activity",
            idea_id=idea_id,
            message=f"Review decision: {response}",
            kind="decision",
        )

        if response == "approve" and next_phase:
            await self._transition(idea_id, next_phase)
            return next_phase
        elif response == "iterate" and agent_phase and iteration < MAX_ITERATIONS:
            await self._transition(idea_id, agent_phase)
            return agent_phase
        elif response == "kill":
            await self._transition(idea_id, Phase.KILLED)
            return Phase.KILLED
        elif response == "pause":
            await self._transition(idea_id, Phase.PAUSED)
            return Phase.PAUSED
        elif response == "timeout":
            await self.dispatcher.notify(f"⏰ *{idea_id}* review timed out, pausing.")
            await self._transition(idea_id, Phase.PAUSED)
            return Phase.PAUSED
        else:
            if recommendation == "proceed" and next_phase:
                await self._transition(idea_id, next_phase)
                return next_phase
            return None

    async def _transition(self, idea_id: str, to_phase: Phase) -> None:
        status = self.blackboard.get_status(idea_id)
        from_phase = Phase(status["phase"])

        if not can_transition(from_phase, to_phase):
            raise ValueError(f"Invalid transition: {from_phase.value} -> {to_phase.value}")

        self.blackboard.set_phase(idea_id, to_phase)
        await self.dispatcher.notify_phase_transition(idea_id, from_phase.value, to_phase.value)
        await _broadcast(
            "phase_transition",
            idea_id=idea_id,
            from_phase=from_phase.value,
            to_phase=to_phase.value,
        )
        logger.info("Transitioned %s: %s -> %s", idea_id, from_phase.value, to_phase.value)

    async def resume(self, idea_id: str) -> None:
        """Resume a paused idea — sets phase so pool picks it up."""
        status = self.blackboard.get_status(idea_id)
        current = Phase(status["phase"])
        if current != Phase.PAUSED:
            raise ValueError(f"Idea {idea_id} is not paused (phase: {current.value})")

        history = status.get("phase_history", [])
        last_agent_phase = None
        for entry in reversed(history):
            p = Phase(entry["from"])
            if p in PHASE_TO_AGENT:
                last_agent_phase = p
                break

        if last_agent_phase:
            await self._transition(idea_id, last_agent_phase)

    async def kill(self, idea_id: str) -> None:
        """Kill an idea."""
        await self._transition(idea_id, Phase.KILLED)

    # ── Priority scoring ──────────────────────────────────────────────

    TERMINAL_PHASES = {Phase.KILLED}

    def _get_active_ideas(self) -> list[dict]:
        """Return status dicts for all non-terminal ideas."""
        active = []
        for idea_id in self.blackboard.list_ideas():
            status = self.blackboard.get_status(idea_id)
            if Phase(status["phase"]) not in self.TERMINAL_PHASES:
                active.append(status)
        return active

    async def score_priorities(self) -> None:
        """Score all active ideas using the Agent SDK for prioritization."""
        from claude_agent_sdk import (
            query, ClaudeAgentOptions,
            AssistantMessage, ResultMessage, TextBlock,
        )

        active = self._get_active_ideas()
        if not active:
            logger.info("No active ideas to score")
            return

        for status in active:
            idea_id = status["id"]
            try:
                idea_md = self.blackboard.read_file(idea_id, "idea.md")
            except FileNotFoundError:
                idea_md = status.get("title", idea_id)

            # Gather extra context from research/feasibility if available
            extra_context = ""
            for filename in ("research.md", "feasibility.md", "competitive-analysis.md"):
                try:
                    content = self.blackboard.read_file(idea_id, filename)
                    if content.strip() and len(content.strip().split("\n")) > 1:
                        extra_context += f"\n\n## {filename}\n{content}"
                except FileNotFoundError:
                    pass

            prompt = (
                "You are a startup idea prioritizer. Score this idea on three dimensions.\n\n"
                f"## Idea\n{idea_md}\n"
                f"{extra_context}\n\n"
                f"Current phase: {status['phase']}\n"
                f"Iterations so far: {status.get('iteration_count', 0)}\n\n"
                "Respond with ONLY valid JSON (no markdown fences) in this exact format:\n"
                "{\n"
                '  "impact": <1-10>,\n'
                '  "values_alignment": <1-10>,\n'
                '  "probability": <1-10>,\n'
                '  "reasoning": "<1-2 sentence explanation>"\n'
                "}"
            )

            try:
                result_text = ""
                async for message in query(
                    prompt=prompt,
                    options=ClaudeAgentOptions(
                        model="claude-haiku-4-5",
                        max_turns=1,
                        allowed_tools=[],
                        env={"CLAUDECODE": ""},
                    ),
                ):
                    if isinstance(message, ResultMessage) and message.result:
                        result_text = message.result
                    elif isinstance(message, AssistantMessage):
                        for block in (message.content or []):
                            if isinstance(block, TextBlock) and block.text:
                                result_text = block.text

                if not result_text.strip():
                    logger.warning("Empty response scoring %s", idea_id)
                    continue

                # Strip markdown fences if present
                raw = result_text.strip()
                if raw.startswith("```"):
                    raw = "\n".join(raw.split("\n")[1:])
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()

                scores = json.loads(raw)

                impact = scores.get("impact", 5)
                values = scores.get("values_alignment", 5)
                probability = scores.get("probability", 5)
                # Weighted average: impact 40%, values 30%, probability 30%
                priority_score = round(impact * 0.4 + values * 0.3 + probability * 0.3, 1)

                self.blackboard.update_status(
                    idea_id,
                    priority_score=priority_score,
                    priority_impact=impact,
                    priority_values_alignment=values,
                    priority_probability=probability,
                    priority_reasoning=scores.get("reasoning", ""),
                )
                logger.info("Scored %s: priority=%.1f (I=%d V=%d P=%d)",
                            idea_id, priority_score, impact, values, probability)
            except Exception as e:
                logger.error("Failed to score %s: %s", idea_id, e)

    # ── Continuous iteration loop ─────────────────────────────────────

    async def run_continuous(self) -> None:
        """Run ideas through the pipeline continuously.

        Released ideas are sent back to ideation for refinement.
        Each agent enters refinement mode when it detects previous outputs exist.
        """
        loop_count = 0
        logger.info("Starting continuous iteration loop")

        while True:
            loop_count += 1

            # Score priorities periodically
            if loop_count == 1 or loop_count % PRIORITY_SCORE_INTERVAL == 0:
                logger.info("Scoring priorities (loop %d)", loop_count)
                await self.score_priorities()

            active = self._get_active_ideas()
            if not active:
                logger.info("No active ideas, sleeping")
                await asyncio.sleep(CONTINUOUS_SLEEP_SECONDS)
                continue

            # Decide which ideas to iterate
            if len(active) <= MAX_ACTIVE_IDEAS:
                to_iterate = active
            else:
                # Sort by priority score descending, take top N
                active.sort(key=lambda s: s.get("priority_score", 0), reverse=True)
                to_iterate = active[:MAX_ACTIVE_IDEAS]

            idea_ids = [s["id"] for s in to_iterate]
            logger.info("Loop %d: iterating %d/%d ideas: %s",
                        loop_count, len(to_iterate), len(active), idea_ids)

            for status in to_iterate:
                idea_id = status["id"]
                phase = Phase(status["phase"])

                # Transition ideas into their next agent phase
                if phase == Phase.SUBMITTED:
                    try:
                        await self._transition(idea_id, Phase.IDEATION)
                    except ValueError:
                        continue
                elif phase == Phase.RELEASED:
                    # Loop back to ideation for refinement
                    try:
                        await self._transition(idea_id, Phase.IDEATION)
                    except ValueError:
                        continue

                # Run the full pipeline from current position
                try:
                    await self._run_pipeline(idea_id)
                except Exception as e:
                    logger.error("Error running pipeline for %s: %s", idea_id, e)

            await asyncio.sleep(CONTINUOUS_SLEEP_SECONDS)
