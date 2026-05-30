"""Bridge between user free-form text and the v2 responders.

Production wiring: each intent maps to a callable that delegates into the
v2.bot.responders module (or v2.bot.state / v2.bot.commands for cases the
responders don't directly expose). The observability layer has already
monkey-patched FD, DeepSeek and Tavily clients, so a trace event fires
automatically when each external call happens.
"""

from __future__ import annotations

import html as _html
import logging
import re
import time
from typing import Any, Callable, Optional

from v2.observability import emit

logger = logging.getLogger(__name__)

ResponderFn = Callable[[dict[str, Any]], str]


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

def classify(text: str) -> tuple[str, dict[str, Any]]:
    """Run v2.bot.intent.classify if available; fall back to keyword stub.

    Falls back when:
      - the v2 module fails to import / call (no API key, etc.)
      - the LLM call returned `intent == "unknown"` (often a sign the key
        is missing and the v2 module swallowed the error)
    """
    try:
        from v2.bot import intent as bot_intent

        result = bot_intent.classify(text)
        if isinstance(result, dict):
            intent_name = str(result.get("intent") or "")
            if intent_name and intent_name != "unknown":
                args: dict[str, Any] = {}
                for key in ("ticker", "manager", "etf", "direction"):
                    v = result.get(key)
                    if v:
                        args[key] = v
                tp = result.get("target_price")
                if tp:
                    try:
                        args["target_price"] = float(tp)
                    except (TypeError, ValueError):
                        pass
                return intent_name, args
    except Exception as exc:
        logger.info("falling back to keyword classifier: %s", exc)

    return _stub_classify(text)


_KEYWORD_MAP: list[tuple[str, str]] = [
    ("为什么", "explain_move"),
    ("why", "explain_move"),
    ("怎么样", "summary"),
    ("summary", "summary"),
    ("产业链", "chain"),
    ("chain", "chain"),
    ("13f", "thirteen_f"),
    ("巴菲特", "thirteen_f"),
    ("buffett", "thirteen_f"),
    ("burry", "thirteen_f"),
    ("谁持有", "holders_view"),
    ("holders", "holders_view"),
    ("ark", "etf_view"),
    ("cathie", "etf_view"),
    ("异动", "find_anomalies"),
    ("anomal", "find_anomalies"),
    ("提醒", "alert_set"),
    ("portfolio", "portfolio_view"),
    ("持仓", "portfolio_view"),
    ("盈亏", "pnl_view"),
    ("pnl", "pnl_view"),
    ("watchlist", "watchlist_view"),
    ("关注", "watchlist_view"),
    ("settings", "settings"),
    ("设置", "settings"),
]


def _stub_classify(text: str) -> tuple[str, dict[str, Any]]:
    lower = text.lower()
    intent_name = "unknown"
    for kw, name in _KEYWORD_MAP:
        if kw in lower:
            intent_name = name
            break
    args: dict[str, Any] = {}
    if intent_name in {"explain_move", "summary", "chain", "holders_view"}:
        ticker = _extract_ticker(text)
        if ticker:
            args["ticker"] = ticker
    return intent_name, args


def _extract_ticker(text: str) -> Optional[str]:
    for token in text.replace(",", " ").replace("?", " ").replace("？", " ").split():
        cleaned = token.strip().upper()
        if 2 <= len(cleaned) <= 5 and cleaned.isascii() and cleaned.isalpha():
            return cleaned
    return None


# ---------------------------------------------------------------------------
# HTML → Markdown — v2 responders emit Telegram HTML; dashboard chat is plain
# ---------------------------------------------------------------------------

def _telegram_html_to_md(s: str) -> str:
    """Convert Telegram-flavored HTML to lightweight Markdown."""
    s = re.sub(r"<b>(.*?)</b>", r"**\1**", s, flags=re.DOTALL)
    s = re.sub(r"<i>(.*?)</i>", r"_\1_", s, flags=re.DOTALL)
    s = re.sub(r"<code>(.*?)</code>", r"`\1`", s, flags=re.DOTALL)
    s = re.sub(r'<a\s+href="([^"]+)">(.*?)</a>', r"[\2](\1)", s, flags=re.DOTALL)
    s = re.sub(r"<[^>]+>", "", s)  # strip anything else
    return _html.unescape(s)


# ---------------------------------------------------------------------------
# Real responder shims — each takes args dict, returns plain text/markdown
# ---------------------------------------------------------------------------

def _r_explain_move(args):
    from v2.bot import responders
    ticker = (args.get("ticker") or "NVDA").upper()
    return _telegram_html_to_md(responders.explain_move(ticker))


def _r_summary(args):
    from v2.bot import responders
    ticker = (args.get("ticker") or "NVDA").upper()
    return _telegram_html_to_md(responders.summary(ticker))


def _r_chain(args):
    from v2.bot import responders
    ticker = (args.get("ticker") or "NVDA").upper()
    return _telegram_html_to_md(responders.chain(ticker))


def _r_thirteen_f(args):
    from v2.bot import responders
    target = args.get("manager") or args.get("ticker") or "brk"
    out = responders.institutional_quick(str(target))
    if isinstance(out, list):
        joined = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(out)
    else:
        joined = str(out)
    return _telegram_html_to_md(joined)


def _r_holders_view(args):
    from v2.bot import responders
    ticker = (args.get("ticker") or "NVDA").upper()
    return _telegram_html_to_md(responders.holders(ticker))


def _r_etf_view(args):
    from v2.bot import responders
    symbol = (args.get("etf") or args.get("ticker") or "ARKK").upper()
    return _telegram_html_to_md(responders.etf_view(symbol))


def _r_find_anomalies(args):
    from v2.bot.commands import _recent_anomalies
    return _telegram_html_to_md(_recent_anomalies())


def _r_settings(args):
    from v2.bot import responders
    return _telegram_html_to_md(responders.settings_view())


def _r_alert_set(args):
    from v2.bot import responders
    ticker = (args.get("ticker") or "").upper()
    target_price = float(args.get("target_price") or 0)
    direction = (args.get("direction") or "above").lower()
    if not ticker or target_price <= 0:
        return "🚫 需要指定 ticker 和 target_price。\n例：「提醒我 NVDA 突破 130」"
    return _telegram_html_to_md(responders.alert_set(ticker, target_price, direction))


def _r_alert_list(args):
    from v2.bot import responders
    return _telegram_html_to_md(responders.alert_list_view())


def _r_alert_remove(args):
    from v2.bot import responders
    try:
        alert_id = int(args.get("alert_id") or args.get("ticker") or 0)
    except (TypeError, ValueError):
        alert_id = 0
    if alert_id <= 0:
        return "🚫 需要指定 alert_id（数字）"
    return _telegram_html_to_md(responders.alert_remove_view(alert_id))


def _r_portfolio_view(args):
    from v2.bot import responders
    return _telegram_html_to_md(responders.portfolio_view())


def _r_pnl_view(args):
    from v2.bot import responders
    return _telegram_html_to_md(responders.pnl_view())


def _r_watchlist_view(args):
    from v2.bot import state
    items = state.watchlist_list()
    if not items:
        return "📋 Watchlist 为空。用 /add TICKER 添加。"
    lines = [f"📋 **Watchlist ({len(items)})**"]
    for it in items:
        lines.append(f"• `{it['ticker']}`  · _added {it['added_at'][:10]}_")
    return "\n".join(lines)


def _r_unknown(args):
    return (
        "🤔 没听懂这个问题。试试：\n"
        "• 「NVDA 为什么跌？」 → explain_move\n"
        "• 「巴菲特最新持仓」 → 13F card\n"
        "• 「Cathie 今天买啥」 → ARKK 当日持仓\n"
        "• 「谁持有 NVDA」 → 反查机构\n"
        "• 「最近有什么异动」 → archive 列表"
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def run_intent(intent: str, args: dict[str, Any]) -> str:
    fn = DISPATCH.get(intent, _r_unknown)
    emit("module_enter", name=fn.__name__, intent=intent, args=args)
    t0 = time.perf_counter()
    try:
        reply = fn(args)
    except Exception as exc:
        emit("error", where=fn.__name__, message=str(exc))
        logger.exception("responder %s failed", fn.__name__)
        reply = f"❌ {fn.__name__} failed: `{type(exc).__name__}: {exc}`"
    finally:
        emit(
            "module_exit",
            name=fn.__name__,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    return reply


DISPATCH: dict[str, ResponderFn] = {
    "explain_move":   _r_explain_move,
    "summary":        _r_summary,
    "chain":          _r_chain,
    "thirteen_f":     _r_thirteen_f,
    "holders_view":   _r_holders_view,
    "etf_view":       _r_etf_view,
    "find_anomalies": _r_find_anomalies,
    "settings":       _r_settings,
    "alert_set":      _r_alert_set,
    "alert_list":     _r_alert_list,
    "alert_remove":   _r_alert_remove,
    "portfolio_view": _r_portfolio_view,
    "pnl_view":       _r_pnl_view,
    "watchlist_view": _r_watchlist_view,
    "unknown":        _r_unknown,
}
