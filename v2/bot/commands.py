"""Stage 1 slash command handlers.

Stage 2 will add: /why /summary /chain /13f /settings (action commands).
Stage 3 will add: NL → intent routing.

All handlers are async (python-telegram-bot 21 is async-only). Long-running
agent work belongs in Stage 2; Stage 1 is fast SQLite-only lookups.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from v2.bot import intent, responders, state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorization — single-user MVP
# ---------------------------------------------------------------------------


def _allowed_chat_id() -> int | None:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def authorized_only(handler):
    """Decorator: drop messages from chat_ids other than the configured owner."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        allowed = _allowed_chat_id()
        chat = update.effective_chat
        if allowed is None or chat is None or chat.id != allowed:
            logger.info("Rejecting message from chat_id=%s",
                        chat.id if chat else "?")
            if chat is not None:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text="❌ Not authorized — this bot is single-user.",
                )
            return
        return await handler(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Help / Start
# ---------------------------------------------------------------------------


_HELP_TEXT = (
    "<b>🤖 Hedge Fund Bot · 命令列表</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Watchlist 管理（Stage 1）</b>\n"
    "  /watchlist          — 查看当前关注列表\n"
    "  /add NVDA           — 添加 ticker\n"
    "  /remove TSLA        — 移除 ticker\n"
    "\n"
    "<b>分析命令</b>\n"
    "  /why NVDA           — 解释最近异动原因\n"
    "  /summary NVDA       — 7 天多维度总结\n"
    "  /chain NVDA         — 产业链邻居\n"
    "  /13f BRK            — Manager 完整组合 + 持仓变动\n"
    "  /holders NVDA       — 哪些机构持有该股票\n"
    "  /etf ARKK           — ARK 基金每日持仓 + 24h 调仓\n"
    "  /earnings AAPL      — 单股财报（下次日期 + 上次结果）\n"
    "  /earnings           — 未来 14 天财报日历（watchlist + 持仓）\n"
    "  /settings           — 查看推送阈值\n"
    "\n"
    "<b>盘中提醒</b>\n"
    "  /alert NVDA 130 above   — 突破/跌破提醒（默认 above）\n"
    "  /alerts                 — 查看未触发提醒\n"
    "  /alert_remove ID        — 删除一条提醒\n"
    "\n"
    "<b>Alpaca 账户（paper）</b>\n"
    "  /portfolio          — 当前持仓 + 现金\n"
    "  /pnl [day|week|month] — 盈亏（默认 day）\n"
    "  /risk               — 组合风险快照（集中度 / 暴露 / 回撤 / 7d 财报）\n"
    "\n"
    "<b>SEC 监控</b>\n"
    "  /8k TICKER          — 最近 30 天 8-K 申报（含 5.02 LLM 抽取）\n"
    "  /insiders TICKER [DAYS] — 内部人交易摘要（默认 90 天）\n"
    "\n"
    "<b>自然语言（Stage 3 即将上线）</b>\n"
    "  直接发问，bot 会自动路由到对应工具\n"
    "  例：「NVDA 为什么涨」「找一下 AMD 的产业链」\n"
    "\n"
    "<i>本 bot 受单用户授权——只响应所有者的消息。</i>"
)


@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(_HELP_TEXT)


@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(_HELP_TEXT)


# ---------------------------------------------------------------------------
# Watchlist commands
# ---------------------------------------------------------------------------


@authorized_only
async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = state.watchlist_list()
    if not items:
        await update.message.reply_html(
            "<b>📋 Watchlist 为空</b>\n"
            "用 <code>/add TICKER</code> 添加股票。"
        )
        return

    lines = [f"<b>📋 Watchlist ({len(items)})</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for it in items:
        added = it["added_at"][:10]  # YYYY-MM-DD
        note = f" — <i>{html.escape(it['note'])}</i>" if it.get("note") else ""
        lines.append(f"• <b>{html.escape(it['ticker'])}</b>  "
                     f"<code>{added}</code>{note}")
    await update.message.reply_html("\n".join(lines))


@authorized_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html(
            "用法：<code>/add TICKER</code>\n"
            "例：<code>/add NVDA</code>"
        )
        return

    ticker = context.args[0].upper()
    try:
        added = state.watchlist_add(ticker)
    except ValueError as exc:
        await update.message.reply_html(
            f"❌ Invalid ticker <code>{html.escape(ticker)}</code>: {exc}"
        )
        return

    if not added:
        await update.message.reply_html(
            f"ℹ️ <b>{html.escape(ticker)}</b> already in watchlist."
        )
        return

    count = len(state.watchlist_list())
    await update.message.reply_html(
        f"✅ Added <b>{html.escape(ticker)}</b> · watchlist size = {count}"
    )


@authorized_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html(
            "用法：<code>/remove TICKER</code>"
        )
        return

    ticker = context.args[0].upper()
    removed = state.watchlist_remove(ticker)
    if not removed:
        await update.message.reply_html(
            f"ℹ️ <b>{html.escape(ticker)}</b> not in watchlist."
        )
        return

    count = len(state.watchlist_list())
    await update.message.reply_html(
        f"🗑 Removed <b>{html.escape(ticker)}</b> · watchlist size = {count}"
    )


# ---------------------------------------------------------------------------
# Stage 2 — action commands (FD + LLM heavy)
# ---------------------------------------------------------------------------


async def _run_blocking(func, *args):
    """Run a synchronous responder in the default thread executor so we don't
    block the bot's event loop while FD / Tavily / DeepSeek calls are in flight."""
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


@authorized_only
async def cmd_why(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html("用法：<code>/why TICKER</code>")
        return
    ticker = context.args[0].upper()
    placeholder = await update.message.reply_html(
        f"🔍 正在分析 <b>{html.escape(ticker)}</b> 最近异动...\n"
        "<i>预计 15-25 秒</i>"
    )
    result = await _run_blocking(responders.explain_move, ticker)
    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html("用法：<code>/summary TICKER</code>")
        return
    ticker = context.args[0].upper()
    placeholder = await update.message.reply_html(
        f"📊 汇总 <b>{html.escape(ticker)}</b> 多维度数据..."
    )
    result = await _run_blocking(responders.summary, ticker)
    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html("用法：<code>/chain TICKER</code>")
        return
    ticker = context.args[0].upper()
    placeholder = await update.message.reply_html(
        f"🕸 正在挖掘 <b>{html.escape(ticker)}</b> 产业链...\n"
        "<i>预计 30-45 秒</i>"
    )
    result = await _run_blocking(responders.chain, ticker)
    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_13f(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html(
            "用法：<code>/13f MANAGER</code>\n"
            "示例：<code>/13f brk</code> · <code>/13f burry</code> · "
            "<code>/13f ark</code>"
        )
        return
    name = " ".join(context.args)
    placeholder = await update.message.reply_html(
        f"🏛 拉取 <b>{html.escape(name)}</b> 最新 13F..."
    )
    messages = await _run_blocking(responders.institutional_quick, name)

    if not messages:
        await placeholder.edit_text("⚠️ 无返回", parse_mode="HTML")
        return

    # First message replaces the placeholder; rest are sent as new messages
    await placeholder.edit_text(messages[0], parse_mode="HTML",
                                 disable_web_page_preview=True)
    for msg in messages[1:]:
        await update.message.reply_html(msg, disable_web_page_preview=True)


@authorized_only
async def cmd_holders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html(
            "用法：<code>/holders TICKER</code>\n"
            "示例：<code>/holders NVDA</code>"
        )
        return
    ticker = context.args[0].upper()
    placeholder = await update.message.reply_html(
        f"🏛 查询 <b>{html.escape(ticker)}</b> 的机构持有人分布..."
    )
    result = await _run_blocking(responders.holders, ticker)
    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_etf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html(
            "用法：<code>/etf SYMBOL</code>\n"
            "示例：<code>/etf ARKK</code> · <code>/etf ARKG</code>"
        )
        return
    symbol = context.args[0].upper()
    placeholder = await update.message.reply_html(
        f"📈 拉取 <b>{html.escape(symbol)}</b> 最新每日持仓..."
    )
    result = await _run_blocking(responders.etf_view, symbol)
    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_html(
            "用法：<code>/alert TICKER PRICE [above|below]</code>\n"
            "示例：<code>/alert NVDA 130 above</code> · "
            "<code>/alert AAPL 200 below</code>"
        )
        return
    ticker = context.args[0].upper()
    try:
        target_price = float(context.args[1])
    except ValueError:
        await update.message.reply_html(
            f"<b>🚫 价格必须是数字：</b> <code>{html.escape(context.args[1])}</code>"
        )
        return
    direction = (context.args[2].lower() if len(context.args) >= 3 else "above")
    result = await _run_blocking(
        responders.alert_set, ticker, target_price, direction,
    )
    await update.message.reply_html(result)


@authorized_only
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await _run_blocking(responders.alert_list_view)
    await update.message.reply_html(result)


@authorized_only
async def cmd_alert_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_html("用法：<code>/alert_remove ID</code>")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.message.reply_html(
            f"<b>🚫 ID 必须是数字：</b> <code>{html.escape(context.args[0])}</code>"
        )
        return
    result = await _run_blocking(responders.alert_remove_view, alert_id)
    await update.message.reply_html(result)


@authorized_only
async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    placeholder = await update.message.reply_html("💼 拉取 Alpaca 账户...")
    result = await _run_blocking(responders.portfolio_view)
    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/pnl [day | week | month]`` — default ``day`` matches the
    pre-Phase-2 behavior exactly. ``week`` / ``month`` route to the
    period responder which reads portfolio_history.
    """
    args = context.args or []
    period = (args[0].strip().lower() if args else "day")

    if period == "day":
        placeholder = await update.message.reply_html("📊 计算盈亏...")
        result = await _run_blocking(responders.pnl_view)
    elif period in ("week", "month"):
        label = "本周" if period == "week" else "本月"
        placeholder = await update.message.reply_html(
            f"📊 计算{label}盈亏..."
        )
        result = await _run_blocking(
            responders.pnl_period, {"period": period},
        )
    else:
        await update.message.reply_html(
            f"<b>🚫 未知周期：</b> <code>{html.escape(period)}</code>\n"
            "用法：<code>/pnl</code> · <code>/pnl day</code> · "
            "<code>/pnl week</code> · <code>/pnl month</code>"
        )
        return

    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/risk`` — real-time portfolio risk card.

    Builds a fresh RiskReport via Alpaca (positions + portfolio_history)
    and yfinance (held-position earnings ≤ 7d). Read-only: no archive
    write, no priority computed."""
    placeholder = await update.message.reply_html(
        "💼 拉取组合风险快照...\n<i>预计 5-10 秒</i>"
    )
    result = await _run_blocking(responders.risk_view, {})
    await placeholder.edit_text(result, parse_mode="HTML",
                                 disable_web_page_preview=True)


@authorized_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await _run_blocking(responders.settings_view)
    await update.message.reply_html(text)


@authorized_only
async def cmd_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/earnings [TICKER]`` — single-ticker card, or 14-day calendar.

    With no args: returns the calendar across watchlist ∪ Alpaca holdings.
    With a ticker: returns the next-release + last-filing card.
    """
    args = context.args or []
    if not args:
        placeholder = await update.message.reply_html("📅 拉取财报日历...")
        result = await _run_blocking(
            responders.earnings_calendar, {"days_horizon": 14},
        )
    else:
        ticker = args[0].upper()
        placeholder = await update.message.reply_html(
            f"📞 查询 <b>{html.escape(ticker)}</b> 财报..."
        )
        result = await _run_blocking(
            responders.earnings_view, {"ticker": ticker},
        )
    await placeholder.edit_text(
        result, parse_mode="HTML", disable_web_page_preview=True,
    )


@authorized_only
async def cmd_8k(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/8k TICKER`` — last-30-days 8-K summary for one ticker.

    Reuses the cron's 5.02 LLM extractor. May take 5-10s when 5.02 is
    present (LLM call); empty/no-5.02 results return in <2s.
    """
    if not context.args:
        await update.message.reply_html(
            "用法：<code>/8k TICKER</code>\n例：<code>/8k AAPL</code>"
        )
        return
    ticker = context.args[0].upper()
    placeholder = await update.message.reply_html(
        f"📋 拉取 <b>{html.escape(ticker)}</b> 最近 30 天 8-K...\n"
        "<i>预计 5-10 秒（含 5.02 LLM 抽取）</i>"
    )
    result = await _run_blocking(
        responders.eight_k_view, {"ticker": ticker},
    )
    await placeholder.edit_text(
        result, parse_mode="HTML", disable_web_page_preview=True,
    )


@authorized_only
async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/macro`` — real-time macro dashboard.

    No args. Pulls live VIX/yields/calendar via build_macro_snapshot
    + release_calendar lookups. Read-only: no archive write, no
    priority computed.
    """
    placeholder = await update.message.reply_html(
        "🌐 拉取宏观 dashboard...\n"
        "<i>VIX / 收益率 / 最近 release (预计 3-5 秒)</i>"
    )
    result = await _run_blocking(responders.macro_view, {})
    await placeholder.edit_text(
        result, parse_mode="HTML", disable_web_page_preview=True,
    )


_RELEASE_LABELS = {
    "CPI": "📈 CPI · 通胀",
    "PCE": "📈 PCE · 通胀",
    "NFP": "📈 NFP · 就业",
    "GDP": "📈 GDP · 经济增长",
    "PPI": "📈 PPI · 生产者物价",
    "Claims": "📈 Initial Claims · 失业金申请",
    "FOMC": "🏛 FOMC · Fed 利率决议",
}


async def _release_check_handler(
    update: Update, release_type: str, *,
    needs_llm: bool = True,
) -> None:
    """Backend shared by /cpi /pce /nfp /gdp /ppi /claims /fomc.

    ``release_type`` MUST be one of the closed enum values used by
    the responder ("CPI" / "PCE" / "NFP" / "GDP" / "PPI" / "Claims"
    / "FOMC"). FOMC takes longer because the responder runs Python
    statement diff + Tavily aggregate (Layer 3 path).
    """
    label = _RELEASE_LABELS.get(release_type, release_type)
    eta_blurb = (
        "<i>预计 8-15 秒 (含 FRED + LLM template-fill + Tavily)</i>"
        if needs_llm else "<i>预计 5-8 秒 (FRED + LLM)</i>"
    )
    placeholder = await update.message.reply_html(
        f"{label} 拉取中...\n{eta_blurb}"
    )
    result = await _run_blocking(
        responders.release_check, {"release_type": release_type.lower()},
    )
    await placeholder.edit_text(
        result, parse_mode="HTML", disable_web_page_preview=True,
    )


@authorized_only
async def cmd_cpi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/cpi`` — latest CPI release with summarizer output."""
    await _release_check_handler(update, "CPI")


@authorized_only
async def cmd_fomc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/fomc`` — most recent FOMC decision (statement diff + SEP +
    Tavily sell-side aggregate). Layer 3 path: no LLM hawkish/dovish
    verdict."""
    await _release_check_handler(update, "FOMC", needs_llm=False)


@authorized_only
async def cmd_yields(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/yields`` — current Treasury curve + 10Y-2Y / 10Y-3M spreads.

    Reuses the macro_view dashboard (the yields panel is the
    information operators want from /yields). Stage 5 may split this
    into a dedicated narrower card.
    """
    placeholder = await update.message.reply_html(
        "🏛 拉取收益率曲线...\n<i>FRED canonical EOD (预计 2-4 秒)</i>"
    )
    result = await _run_blocking(responders.macro_view, {})
    await placeholder.edit_text(
        result, parse_mode="HTML", disable_web_page_preview=True,
    )


@authorized_only
async def cmd_insiders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/insiders TICKER [DAYS]`` — Form 4 summary for one ticker.

    Optional days arg: 7-365, default 90. Pure-Python aggregation
    (no LLM), typically returns in <2s.

    Examples:
        /insiders NVDA
        /insiders NVDA 30
    """
    if not context.args:
        await update.message.reply_html(
            "用法：<code>/insiders TICKER [DAYS]</code>\n"
            "例：<code>/insiders NVDA</code> · <code>/insiders NVDA 30</code>"
        )
        return
    ticker = context.args[0].upper()
    args_dict: dict = {"ticker": ticker}
    if len(context.args) >= 2:
        try:
            days = int(context.args[1])
            args_dict["days_back"] = days
        except ValueError:
            await update.message.reply_html(
                f"<b>🚫 DAYS 必须是数字：</b> "
                f"<code>{html.escape(context.args[1])}</code>"
            )
            return
    placeholder = await update.message.reply_html(
        f"📥 拉取 <b>{html.escape(ticker)}</b> 内部人交易..."
    )
    result = await _run_blocking(responders.insider_view, args_dict)
    await placeholder.edit_text(
        result, parse_mode="HTML", disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Stage 3 — NL → Intent classifier and router
# ---------------------------------------------------------------------------


_INTENT_DISPLAY = {
    "explain_move":     "💡 解释异动",
    "summary":          "📊 综合概览",
    "chain":            "🕸 产业链",
    "thirteen_f":       "🏛 机构 13F",
    "holders_view":     "🏛 机构持有人",
    "etf_view":         "📈 ARK 每日持仓",
    "watchlist_view":   "📋 查看 watchlist",
    "watchlist_add":    "➕ 添加 watchlist",
    "watchlist_remove": "🗑 移除 watchlist",
    "settings":         "⚙️ 设置",
    "find_anomalies":   "🚨 最近异动",
    "alert_set":        "🔔 设置价格提醒",
    "alert_list":       "🔔 查看价格提醒",
    "portfolio_view":   "💼 Alpaca 持仓",
    "pnl_view":         "📊 当日盈亏",
    "earnings_view":    "📞 财报详情",
    "earnings_calendar":"📅 财报日历",
    "risk_view":        "💼 组合风险",
    "pnl_period":       "📊 周期盈亏",
    "eight_k_view":     "📋 SEC 8-K",
    "insider_view":     "📥 内部人交易",
    "macro_view":       "🌐 宏观 dashboard",
    "release_check":    "📈 宏观 release",
    "unknown":          "❓ 未识别",
}


@authorized_only
async def cmd_nl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stage 3: classify NL, route to the same responder as the equivalent slash command."""
    text = (update.message.text or "").strip()
    if not text:
        return

    placeholder = await update.message.reply_html("🤔 理解中...")

    parsed = await _run_blocking(intent.classify, text)
    name = parsed["intent"]
    ticker = parsed["ticker"]
    manager = parsed["manager"]
    etf = parsed.get("etf", "")
    target_price = parsed.get("target_price", 0.0) or 0.0
    direction = parsed.get("direction", "") or "above"
    days_horizon = parsed.get("days_horizon", 0) or 0
    period = parsed.get("period", "") or ""
    days_back = parsed.get("days_back", 0) or 0
    release_type = parsed.get("release_type", "") or ""

    # Tell the user how we routed — transparency builds trust
    routing_chip = (
        f"<i>🎯 识别为：{_INTENT_DISPLAY.get(name, name)}"
        + (f" · <code>{html.escape(ticker)}</code>" if ticker else "")
        + (f" · <code>{html.escape(manager)}</code>" if manager else "")
        + (f" · <code>{html.escape(etf)}</code>" if etf else "")
        + "</i>\n\n"
    )

    try:
        if name == "explain_move":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 无法识别 ticker，请明确股票代码或公司名。",
                    parse_mode="HTML",
                )
                return
            result = await _run_blocking(responders.explain_move, ticker)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "summary":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 无法识别 ticker。",
                    parse_mode="HTML",
                )
                return
            result = await _run_blocking(responders.summary, ticker)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "chain":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 无法识别 ticker。",
                    parse_mode="HTML",
                )
                return
            result = await _run_blocking(responders.chain, ticker)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "thirteen_f":
            if not manager:
                await placeholder.edit_text(
                    routing_chip + "❓ 无法识别 manager 名称。",
                    parse_mode="HTML",
                )
                return
            messages = await _run_blocking(responders.institutional_quick, manager)
            if not messages:
                await placeholder.edit_text(routing_chip + "⚠️ 无返回",
                                             parse_mode="HTML")
                return
            await placeholder.edit_text(routing_chip + messages[0],
                                         parse_mode="HTML",
                                         disable_web_page_preview=True)
            for msg in messages[1:]:
                await update.message.reply_html(msg,
                                                 disable_web_page_preview=True)

        elif name == "holders_view":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 无法识别 ticker。",
                    parse_mode="HTML",
                )
                return
            result = await _run_blocking(responders.holders, ticker)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "etf_view":
            symbol = etf or "ARKK"  # default to ARKK if ambiguous
            result = await _run_blocking(responders.etf_view, symbol)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "watchlist_view":
            items = state.watchlist_list()
            if not items:
                await placeholder.edit_text(
                    routing_chip + "<b>📋 Watchlist 为空</b>",
                    parse_mode="HTML",
                )
            else:
                tickers = ", ".join(html.escape(it["ticker"]) for it in items)
                await placeholder.edit_text(
                    routing_chip + f"<b>📋 Watchlist ({len(items)})</b>\n{tickers}",
                    parse_mode="HTML",
                )

        elif name == "watchlist_add":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 没说要加哪只股票。",
                    parse_mode="HTML",
                )
                return
            try:
                added = state.watchlist_add(ticker)
            except ValueError:
                await placeholder.edit_text(
                    routing_chip + f"❌ Invalid ticker: <code>{html.escape(ticker)}</code>",
                    parse_mode="HTML",
                )
                return
            msg = (f"✅ Added <b>{html.escape(ticker)}</b>"
                   if added else
                   f"ℹ️ <b>{html.escape(ticker)}</b> 已在 watchlist")
            await placeholder.edit_text(routing_chip + msg, parse_mode="HTML")

        elif name == "watchlist_remove":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 没说要移除哪只股票。",
                    parse_mode="HTML",
                )
                return
            removed = state.watchlist_remove(ticker)
            msg = (f"🗑 Removed <b>{html.escape(ticker)}</b>"
                   if removed else
                   f"ℹ️ <b>{html.escape(ticker)}</b> 不在 watchlist")
            await placeholder.edit_text(routing_chip + msg, parse_mode="HTML")

        elif name == "settings":
            text_out = await _run_blocking(responders.settings_view)
            await placeholder.edit_text(routing_chip + text_out, parse_mode="HTML")

        elif name == "find_anomalies":
            recent = await _run_blocking(_recent_anomalies)
            await placeholder.edit_text(routing_chip + recent, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "alert_set":
            if not ticker or target_price <= 0:
                await placeholder.edit_text(
                    routing_chip + "❓ 没说清楚 ticker 或目标价。\n"
                    "示例：「提醒我 NVDA 突破 130」",
                    parse_mode="HTML",
                )
                return
            result = await _run_blocking(
                responders.alert_set, ticker, target_price,
                direction if direction in ("above", "below") else "above",
            )
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML")

        elif name == "alert_list":
            result = await _run_blocking(responders.alert_list_view)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML")

        elif name == "portfolio_view":
            result = await _run_blocking(responders.portfolio_view)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "pnl_view":
            result = await _run_blocking(responders.pnl_view)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "earnings_view":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 无法识别 ticker，请明确股票代码或公司名。",
                    parse_mode="HTML",
                )
                return
            result = await _run_blocking(
                responders.earnings_view, {"ticker": ticker},
            )
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "earnings_calendar":
            args = {"days_horizon": days_horizon} if days_horizon else {}
            result = await _run_blocking(responders.earnings_calendar, args)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "risk_view":
            result = await _run_blocking(responders.risk_view, {})
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "pnl_period":
            # Empty string from intent classifier → responder default (day)
            args = {"period": period} if period else {}
            result = await _run_blocking(responders.pnl_period, args)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "eight_k_view":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 没说要查哪只股票的 8-K。",
                    parse_mode="HTML",
                )
                return
            result = await _run_blocking(
                responders.eight_k_view, {"ticker": ticker},
            )
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "insider_view":
            if not ticker:
                await placeholder.edit_text(
                    routing_chip + "❓ 没说要查哪只股票的内部人交易。",
                    parse_mode="HTML",
                )
                return
            args_d: dict = {"ticker": ticker}
            if days_back:
                args_d["days_back"] = days_back
            result = await _run_blocking(responders.insider_view, args_d)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "macro_view":
            result = await _run_blocking(responders.macro_view, {})
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        elif name == "release_check":
            args_d2: dict = {}
            if release_type:
                args_d2["release_type"] = release_type
            result = await _run_blocking(responders.release_check, args_d2)
            await placeholder.edit_text(routing_chip + result, parse_mode="HTML",
                                         disable_web_page_preview=True)

        else:  # "unknown"
            await placeholder.edit_text(
                routing_chip
                + "🤔 没听懂这个问题。可以试试：\n"
                "  · 「NVDA 为什么跌？」\n"
                "  · 「看看 AAPL」\n"
                "  · 「找一下 AMD 的产业链」\n"
                "  · 「巴菲特最近买了什么」\n"
                "  · 「我的关注列表」\n"
                "或直接用 <code>/help</code> 看完整命令。",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.exception("NL routing failed for %r", text)
        await placeholder.edit_text(
            routing_chip + f"❌ Error: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


def _recent_anomalies() -> str:
    """Pull the last 5 anomaly pushes from archive.db."""
    import sqlite3
    from pathlib import Path

    db = Path(__file__).resolve().parents[2] / "data" / "archive.db"
    if not db.exists():
        return "<i>archive.db 不存在</i>"

    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT ts, tickers, text_html FROM pushes
               WHERE agent='anomaly' ORDER BY id DESC LIMIT 5"""
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return f"<i>archive 查询失败: {html.escape(str(exc))}</i>"

    if not rows:
        return "<i>近期 archive 中无异动记录</i>"

    lines = ["<b>🚨 近期异动（archive 最新 5 条）</b>", ""]
    for r in rows:
        ts = (r["ts"] or "")[:19].replace("T", " ")
        tickers = r["tickers"] or "?"
        lines.append(f"  • <code>{html.escape(ts)}</code> · "
                     f"<b>{html.escape(tickers)}</b>")
    lines.append("")
    lines.append("<i>使用 <code>/why TICKER</code> 重新解读任意一条。</i>")
    return "\n".join(lines)
