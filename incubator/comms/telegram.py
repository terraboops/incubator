from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Bidirectional Telegram communication with inline keyboard support."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._app: Application | None = None

    async def start(self) -> None:
        """Start the Telegram bot for receiving callback queries."""
        if not self.bot_token:
            logger.warning("No Telegram bot token configured, notifications disabled")
            return
        self._app = Application.builder().token(self.bot_token).build()
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        logger.info("Telegram bot started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send(self, message: str) -> None:
        """Send a one-way notification."""
        if not self.bot_token:
            logger.info("Telegram (disabled): %s", message)
            return
        from telegram import Bot

        bot = Bot(self.bot_token)
        await bot.send_message(
            chat_id=self.chat_id,
            text=message,
            parse_mode="Markdown",
        )

    async def ask_human(
        self,
        question: str,
        options: list[str],
        timeout_seconds: int = 86400,
    ) -> str:
        """Ask a question with inline keyboard buttons, wait for response."""
        if not self.bot_token:
            logger.info("Telegram ask (disabled): %s [%s]", question, options)
            return options[0] if options else "approve"

        from telegram import Bot

        bot = Bot(self.bot_token)

        # Create a unique ID for this question
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        question_id = str(id(future))
        self._pending[question_id] = future

        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"{question_id}:{opt}") for opt in options]
        ]
        await bot.send_message(
            chat_id=self.chat_id,
            text=question,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("Telegram ask timed out for: %s", question)
            return "timeout"
        finally:
            self._pending.pop(question_id, None)

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            return

        question_id, answer = parts
        future = self._pending.get(question_id)
        if future and not future.done():
            future.set_result(answer)
            await query.edit_message_text(f"✅ You chose: *{answer}*", parse_mode="Markdown")
