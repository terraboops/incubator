from __future__ import annotations

import logging

from incubator.comms.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Thin abstraction over notification channels. Currently Telegram-only."""

    def __init__(self, telegram: TelegramNotifier) -> None:
        self.telegram = telegram

    async def notify(self, message: str) -> None:
        await self.telegram.send(message)

    async def ask(
        self,
        question: str,
        options: list[str],
        timeout_seconds: int = 86400,
    ) -> str:
        return await self.telegram.ask_human(question, options, timeout_seconds)

    async def notify_phase_transition(
        self, idea_id: str, from_phase: str, to_phase: str
    ) -> None:
        await self.notify(
            f"🔄 *{idea_id}*: `{from_phase}` → `{to_phase}`"
        )

    async def notify_error(self, idea_id: str, error: str) -> None:
        await self.notify(f"❌ *{idea_id}* error:\n```\n{error}\n```")
