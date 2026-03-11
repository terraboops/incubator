"""Cron-like scheduler for watcher agents."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from croniter import croniter

from incubator.config import Settings
from incubator.core.lock import LockManager

logger = logging.getLogger(__name__)


class Scheduler:
    """Runs watcher agents on their configured cadences."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock_manager = LockManager()
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self, watchers: list[dict]) -> None:
        """Start scheduled watcher runs."""
        self._running = True
        for watcher in watchers:
            task = asyncio.create_task(
                self._run_on_schedule(
                    watcher["name"],
                    watcher["cron"],
                    watcher["callback"],
                )
            )
            self._tasks.append(task)
        logger.info("Scheduler started with %d watchers", len(watchers))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def _run_on_schedule(self, name: str, cron: str, callback) -> None:
        cron_iter = croniter(cron, datetime.now(timezone.utc))
        while self._running:
            next_run = cron_iter.get_next(datetime)
            now = datetime.now(timezone.utc)
            delay = (next_run - now).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

            if not self._running:
                break

            # Global lock prevents concurrent watcher runs
            if self.lock_manager.acquire("watcher", name, executor="scheduler"):
                try:
                    logger.info("Running watcher: %s", name)
                    await callback()
                except Exception:
                    logger.exception("Watcher %s failed", name)
                finally:
                    self.lock_manager.release("watcher", name)
            else:
                logger.info("Watcher %s skipped (locked)", name)
