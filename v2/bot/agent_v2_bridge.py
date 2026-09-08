"""Production Telegram transport for the explicit ``/ask_v2`` command."""

from __future__ import annotations

import html
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

from v2.agent import bot_bridge as delivery
from v2.agent import presentation
from v2.agent_v2.interfaces.telegram import TelegramFacade, TelegramMessage
from v2.agent_v2.orchestrator import AgentV2Config
from v2.agent_v2.runtime import build_workspace_agent

_AGENT = None
_AGENT_WEB_ENABLED = None
_AGENT_LOCK = threading.Lock()


def _web_enabled() -> bool:
    return os.environ.get("AGENT_V2_WEB_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _get_agent():
    global _AGENT, _AGENT_WEB_ENABLED
    web_enabled = _web_enabled()
    if _AGENT is None or _AGENT_WEB_ENABLED != web_enabled:
        with _AGENT_LOCK:
            if _AGENT is None or _AGENT_WEB_ENABLED != web_enabled:
                _AGENT = build_workspace_agent(
                    config=AgentV2Config(execute_async_inline=True),
                    enable_web=web_enabled,
                )
                _AGENT_WEB_ENABLED = web_enabled
    return _AGENT


class TelegramBotTransport:
    """Adapt python-telegram-bot objects to the framework-neutral facade."""

    def __init__(self, context: Any, placeholder: Any, *, web_requested: bool) -> None:
        self.context = context
        self.placeholder = placeholder
        self.web_requested = web_requested
        self._last_progress_at = 0.0
        self._last_status = ""

    async def typing(self, chat_id: int) -> None:
        try:
            await self.context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:  # noqa: BLE001 — typing is cosmetic
            pass

    async def progress(self, chat_id: int, event) -> None:
        now = time.monotonic()
        status = event.status.value
        if status == self._last_status and now - self._last_progress_at < 4:
            return
        self._last_status = status
        self._last_progress_at = now
        message = html.escape(event.message or status)
        try:
            await self.placeholder.edit_text(
                f"<b>Agent V2 · {html.escape(status)}</b>\n<i>{message}</i>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001 — final delivery still matters
            pass

    async def deliver(self, chat_id: int, result) -> None:
        web_state = "已启用" if self.web_requested and _web_enabled() else "未启用"
        header = f"<b>Agent V2 · {html.escape(result.status.value)}</b>\n" f"<i>路径：{html.escape(result.route.kind.value)} · " f"回答：{html.escape(result.answer_mode.value)} · 网页：{web_state}</i>\n\n"
        answer = presentation.to_telegram_html(result.answer)
        sources = []
        seen = set()
        for item in result.evidence:
            if not item.source_url or item.source_url in seen:
                continue
            if urlparse(item.source_url).scheme not in {"http", "https"}:
                continue
            seen.add(item.source_url)
            label = html.escape(item.source_title or item.source_id or item.source_url)
            url = html.escape(item.source_url, quote=True)
            sources.append(f'• <a href="{url}">{label}</a>')
            if len(sources) >= 5:
                break
        suffix = "\n\n<b>来源</b>\n" + "\n".join(sources) if sources else ""
        await delivery._deliver(self.placeholder, header + answer + suffix)


async def handle_agent_v2(
    update: Any,
    context: Any,
    text: str,
    *,
    allow_web: bool = False,
) -> Any:
    """Run one explicit Agent V2 request without touching normal NL routing."""

    chat = update.effective_chat
    message = update.message
    placeholder = await message.reply_html("🧭 Agent V2 正在规划...")
    transport = TelegramBotTransport(
        context,
        placeholder,
        web_requested=allow_web,
    )
    return await TelegramFacade(_get_agent()).handle(
        TelegramMessage(
            chat_id=chat.id,
            text=text,
            message_id=getattr(message, "message_id", None),
        ),
        transport,
        allow_web=allow_web,
    )
