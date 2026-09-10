"""Production Telegram transport for Agent V2: plain messages and ``/ask_v2``."""

from __future__ import annotations

import html
import os
import threading
import time
from typing import Any

from v2.agent import bot_bridge as delivery
from v2.agent import presentation
from v2.agent_v2.interfaces import telegram_format
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


def web_default() -> bool:
    """Whether a plain message may use the web unless it says ``--noweb``.

    The bot answers one authorised owner, so the consent the web page asks
    for with a checkbox is given once here, by configuration.  The server
    flag ``AGENT_V2_WEB_ENABLED`` stays the master switch.
    """

    return os.environ.get("TELEGRAM_WEB_DEFAULT", "1").strip().lower() not in {"0", "false", "no", "off"}


def split_web_consent(text: str) -> tuple[str, bool]:
    """Take ``--web`` / ``--noweb`` out of a message; what is left is the question.

    ``--web`` always allows the web for this message and ``--noweb`` always
    forbids it; without either, :func:`web_default` decides.
    """

    tokens = text.split()
    lowered = [token.lower() for token in tokens]
    if "--noweb" in lowered:
        allow_web = False
    elif "--web" in lowered:
        allow_web = True
    else:
        allow_web = web_default()
    question = " ".join(token for token in tokens if token.lower() not in {"--web", "--noweb"}).strip()
    return question, allow_web


def _get_agent():
    global _AGENT, _AGENT_WEB_ENABLED
    web_enabled = _web_enabled()
    if _AGENT is None or _AGENT_WEB_ENABLED != web_enabled:
        with _AGENT_LOCK:
            if _AGENT is None or _AGENT_WEB_ENABLED != web_enabled:
                _AGENT = build_workspace_agent(
                    config=AgentV2Config(),
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
        header = self._header(result)
        numbered = telegram_format.number_citations(
            telegram_format.compact_attributions(result.answer, result),
            result.evidence,
        )
        answer = presentation.to_telegram_html(numbered.text)
        # The sub-agent notes (the debater's objections) cite evidence too:
        # they continue the answer's numbering and their sources join the list.
        order = list(numbered.ids)
        agents = [telegram_format.number_citations(line, result.evidence, order=order).text for line in telegram_format.agent_lines(result)]
        sources = []
        for entry in telegram_format.source_entries(tuple(order), result.evidence):
            label = html.escape(entry.label)
            if entry.url:
                label = f'<a href="{html.escape(entry.url, quote=True)}">{label}</a>'
            sources.append(f"{html.escape(entry.numbers)}. {label}")
        suffix = "\n\n<b>来源</b>\n" + "\n".join(sources) if sources else ""
        if agents:
            suffix += "\n\n<b>子智能体</b>\n" + "\n".join(html.escape(line) for line in agents)
        await delivery._deliver(self.placeholder, header + answer + suffix)

    def _header(self, result) -> str:
        fields = [
            f"路径：{html.escape(result.route.kind.value)}",
            f"回答：{html.escape(result.answer_mode.value)}",
        ]
        synthesis = telegram_format.synthesis_label(result)
        if synthesis:
            note = telegram_format.completion_note(result)
            fields.append(f"合成：{html.escape(synthesis + (f'，{note}' if note else ''))}")
        fields.append(f"校验：{html.escape(telegram_format.verification_label(result))}")
        fields.append(
            "网页：" + html.escape(telegram_format.web_label(requested=self.web_requested, enabled=_web_enabled()))
        )
        lines = [f"<b>Agent V2 · {html.escape(result.status.value)}</b>", f"<i>{' · '.join(fields)}</i>"]
        warning = telegram_format.warning_line(result)
        if warning:
            lines.append(f"<i>⚠ 校验：{html.escape(warning)}</i>")
        budget = telegram_format.budget_line(result)
        if budget:
            lines.append(f"<i>⏱ {html.escape(budget)}</i>")
        reason = telegram_format.fallback_reason(result)
        if reason:
            lines.append(f"<i>兜底原因：{html.escape(reason)}</i>")
        repaired = telegram_format.repair_reason(result)
        if repaired:
            lines.append(f"<i>修正原因：{html.escape(repaired)}</i>")
        return "\n".join(lines) + "\n\n"


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
