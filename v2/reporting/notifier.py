"""Notifier protocol and concrete implementations.

The Protocol defines what it means to "push a report somewhere".
Any class with these methods can be passed wherever a Notifier is expected.

Implementations:
    ConsoleNotifier  — prints to stdout, for local debugging
    TelegramNotifier — pushes to a Telegram chat via Bot API

To add a new channel (Feishu, Discord, Slack), just write a class with the
two methods — no inheritance needed.
"""

from __future__ import annotations

import asyncio
import os
from io import BytesIO
from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """Anything that can push a text message and a photo."""

    def send_text(self, text: str) -> None: ...
    def send_photo(self, image: bytes, caption: str = "") -> None: ...


class ConsoleNotifier:
    """Local fallback — prints to stdout. Use for development."""

    def send_text(self, text: str) -> None:
        print("─" * 60)
        print(text)
        print("─" * 60)

    def send_photo(self, image: bytes, caption: str = "") -> None:
        print(f"[image: {len(image):,} bytes] {caption}")


class TelegramNotifier:
    """Push to a Telegram chat via Bot API.

    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env if not given.
    Uses HTML parse mode (simpler escaping than MarkdownV2).

    If an *archive* is given, every send_* call also writes a row to the
    local archive DB before pushing to Telegram (so failed pushes still
    leave a paper trail).
    """

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        *,
        archive=None,
    ) -> None:
        self._token = token or os.environ["TELEGRAM_BOT_TOKEN"]
        self._chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
        self._archive = archive

    def send_text(self, text: str) -> None:
        if self._archive is not None:
            self._archive.save_text(text)
        asyncio.run(self._send_text(text))

    def send_photo(self, image: bytes, caption: str = "") -> None:
        if self._archive is not None:
            self._archive.save_photo(image, caption)
        asyncio.run(self._send_photo(image, caption))

    async def _send_text(self, text: str) -> None:
        from telegram import Bot

        bot = Bot(token=self._token)
        async with bot:
            await bot.send_message(
                chat_id=self._chat_id, text=text, parse_mode="HTML",
            )

    async def _send_photo(self, image: bytes, caption: str) -> None:
        from telegram import Bot

        bot = Bot(token=self._token)
        async with bot:
            await bot.send_photo(
                chat_id=self._chat_id,
                photo=BytesIO(image),
                caption=caption,
                parse_mode="HTML",
            )
