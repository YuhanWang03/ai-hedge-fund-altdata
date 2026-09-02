"""Map a classified NL intent to an HTML reply (+ optional chart).

Reuses the production v2 responders with the *exact* call conventions the
Telegram bot uses (v2/bot/commands.py:cmd_nl), so the web chat and the bot
return byte-identical cards. Blocking by design — the route runs it in a
threadpool.
"""

from __future__ import annotations

import base64
import html as _html


def _err(msg: str) -> dict:
    return {"html": f"<b>⚠️ {_html.escape(msg)}</b>"}


def dispatch(parsed: dict) -> dict:
    """parsed = output of v2.bot.intent.classify(). Returns a dict with:
    html (str), optional extra_html (list[str]), optional chart_b64 (str)."""
    from v2.bot import responders, state

    intent = parsed.get("intent", "unknown")
    ticker = parsed.get("ticker", "")
    manager = parsed.get("manager", "")
    etf = parsed.get("etf", "")
    period = parsed.get("period", "")
    release_type = parsed.get("release_type", "")
    days_back = parsed.get("days_back", 0)
    days_horizon = parsed.get("days_horizon", 0)

    try:
        if intent == "explain_move":
            return _err("无法识别 ticker") if not ticker else {"html": responders.explain_move(ticker)}
        if intent == "summary":
            return _err("无法识别 ticker") if not ticker else {"html": responders.summary(ticker)}
        if intent == "chain":
            return _err("无法识别 ticker") if not ticker else {"html": responders.chain(ticker)}
        if intent == "thirteen_f":
            if not manager:
                return _err("无法识别 manager 名称")
            msgs = responders.institutional_quick(manager)
            return {"html": msgs[0] if msgs else "⚠️ 无返回", "extra_html": msgs[1:]}
        if intent == "holders_view":
            return _err("无法识别 ticker") if not ticker else {"html": responders.holders(ticker)}
        if intent == "etf_view":
            return {"html": responders.etf_view(etf or "ARKK")}
        if intent == "watchlist_view":
            items = state.watchlist_list()
            if not items:
                return {"html": "<b>📋 Watchlist 为空</b>"}
            tks = ", ".join(_html.escape(it["ticker"]) for it in items)
            return {"html": f"<b>📋 Watchlist ({len(items)})</b>\n{tks}"}
        if intent == "watchlist_add":
            if not ticker:
                return _err("没说要加哪只股票")
            try:
                added = state.watchlist_add(ticker)
            except ValueError:
                return _err(f"Invalid ticker: {ticker}")
            return {"html": f"✅ Added <b>{_html.escape(ticker)}</b>" if added
                    else f"ℹ️ <b>{_html.escape(ticker)}</b> 已在 watchlist"}
        if intent == "watchlist_remove":
            if not ticker:
                return _err("没说要移除哪只股票")
            removed = state.watchlist_remove(ticker)
            return {"html": f"🗑 Removed <b>{_html.escape(ticker)}</b>" if removed
                    else f"ℹ️ <b>{_html.escape(ticker)}</b> 不在 watchlist"}
        if intent == "settings":
            return {"html": responders.settings_view()}
        if intent == "portfolio_view":
            return {"html": responders.portfolio_view()}
        if intent == "pnl_view":
            return {"html": responders.pnl_view()}
        if intent == "pnl_period":
            return {"html": responders.pnl_period({"period": period} if period else {})}
        if intent == "risk_view":
            return {"html": responders.risk_view({})}
        if intent == "earnings_view":
            return _err("无法识别 ticker") if not ticker else {"html": responders.earnings_view({"ticker": ticker})}
        if intent == "earnings_calendar":
            return {"html": responders.earnings_calendar({"days_horizon": days_horizon} if days_horizon else {})}
        if intent == "eight_k_view":
            return _err("没说要查哪只股票的 8-K") if not ticker else {"html": responders.eight_k_view({"ticker": ticker})}
        if intent == "insider_view":
            if not ticker:
                return _err("没说要查哪只股票的内部人交易")
            args = {"ticker": ticker}
            if days_back:
                args["days_back"] = days_back
            return {"html": responders.insider_view(args)}
        if intent == "macro_view":
            return {"html": responders.macro_view({})}
        if intent == "release_check":
            return {"html": responders.release_check({"release_type": release_type} if release_type else {})}
        if intent == "moneyflow_view":
            if not ticker:
                return _err("没说要看哪只股票的资金流")
            caption, chart = responders.moneyflow_view({"ticker": ticker})
            out: dict = {"html": caption}
            if chart:
                out["chart_b64"] = base64.b64encode(chart).decode("ascii")
            return out
        return {"html": "🤔 暂不支持这个查询，换个说法试试，或用左侧面板。"}
    except Exception as exc:  # never 500 the chat on a responder error
        return {"html": f"❌ Error: <code>{_html.escape(str(exc))}</code>"}
