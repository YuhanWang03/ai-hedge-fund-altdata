"""Bridge between user free-form text and the v2 responders.

Real wiring path (production):
    1. classify_intent(text) → Intent(name, args) using v2.bot.intent
       (which the observability layer has already monkey-patched, so a
       trace event fires automatically).
    2. RESPONDER_DISPATCH[intent.name](args) → str reply.
       Each responder is a thin shim that calls the same v2 module the
       Telegram bot's responder does, but skipping the Telegram-specific
       Update/Context wrapping.

The dispatch table below leaves a `_stub_responder` placeholder for
every intent. The placeholder issues a couple of representative trace
events so the dashboard demo is visually complete even on machines where
v2/data and live API keys are not present.

To wire a real responder, replace the entry in DISPATCH with a callable
that takes (args: dict) -> str and uses the underlying v2 module directly.
The observability hooks installed at startup will record every FD / LLM /
Tavily / DB call along the way without further code changes.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from v2.observability import emit

logger = logging.getLogger(__name__)

ResponderFn = Callable[[dict[str, Any]], str]


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

def classify(text: str) -> tuple[str, dict[str, Any]]:
    """Run v2.bot.intent.classify_intent if available; fall back to a
    keyword-based stub when the module can't import (e.g. local dev with
    no DeepSeek key).
    """
    try:
        from v2.bot import intent as bot_intent

        result = bot_intent.classify_intent(text)
        name = getattr(result, "name", None) or (
            result.get("name") if isinstance(result, dict) else None
        )
        args = getattr(result, "args", None) or (
            result.get("args") if isinstance(result, dict) else None
        ) or {}
        if name:
            return str(name), dict(args)
    except Exception as exc:
        logger.info("falling back to stub classifier: %s", exc)

    return _stub_classify(text)


_KEYWORD_MAP: list[tuple[str, str]] = [
    ("为什么", "explain_move"),
    ("why", "explain_move"),
    ("怎么样", "summary"),
    ("怎么", "summary"),
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
    ("settings", "settings"),
    ("设置", "settings"),
]


def _stub_classify(text: str) -> tuple[str, dict[str, Any]]:
    """Pure-Python keyword classifier used when DeepSeek isn't available.

    Pulls the first uppercase token of length 2-5 as the ticker when the
    intent suggests one.
    """
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
# Responder dispatch
# ---------------------------------------------------------------------------

def run_intent(intent: str, args: dict[str, Any]) -> str:
    """Dispatch to the registered responder. Emits events along the way.

    Real responders live in v2.bot.responders but take Telegram Update
    objects, so this layer bridges by calling lower-level v2 modules.
    On a stub deployment (no API keys), `_stub_responder` produces a
    short, visually realistic event trace.
    """
    fn = DISPATCH.get(intent, _stub_responder)
    emit("module_enter", name=fn.__name__, intent=intent, args=args)
    t0 = time.perf_counter()
    try:
        reply = fn(args)
    except Exception as exc:
        emit("error", where=fn.__name__, message=str(exc))
        raise
    finally:
        emit(
            "module_exit",
            name=fn.__name__,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    return reply


def _stub_responder(args: dict[str, Any]) -> str:
    """Demo-only responder. Emits a small handful of plausible events so
    the trace panel renders something interesting without burning real API
    spend. Replace via real wiring in DISPATCH below.
    """
    import random

    ticker = args.get("ticker", "NVDA")

    emit("api_call", provider="fd", endpoint="get_prices",
         ticker=ticker, cache="miss", elapsed_ms=random.randint(200, 500))
    emit("api_call", provider="fd", endpoint="get_fundamentals",
         ticker=ticker, cache="hit", elapsed_ms=random.randint(2, 8))
    emit("api_call", provider="tavily", endpoint="search",
         query=f"{ticker} latest news", num_results=5,
         cost_usd=0.005, elapsed_ms=random.randint(800, 1400))
    emit("llm_call", provider="deepseek", model="deepseek-chat",
         prompt_preview=f"Synthesize attribution for {ticker} ...",
         response_preview="(stub response — wire real responder for full output)",
         input_tokens=1840, output_tokens=220, cost_usd=0.000319,
         elapsed_ms=random.randint(1800, 2400))
    emit("db_write", db="chroma",
         fn="add_attribution", elapsed_ms=random.randint(5, 20))

    return (
        f"⚠️ Stub mode: dashboard responder for `{ticker}` not yet wired.\n"
        "Connect v2 responders in app/runner/intent_adapter.py DISPATCH "
        "to see real attributions."
    )


# Real responders should replace these entries on the production VPS.
# Each must accept a dict and return a str.
DISPATCH: dict[str, ResponderFn] = {
    "explain_move": _stub_responder,
    "summary": _stub_responder,
    "chain": _stub_responder,
    "thirteen_f": _stub_responder,
    "holders_view": _stub_responder,
    "etf_view": _stub_responder,
    "find_anomalies": _stub_responder,
    "settings": _stub_responder,
    "alert_set": _stub_responder,
    "alert_list": _stub_responder,
    "alert_remove": _stub_responder,
    "portfolio_view": _stub_responder,
    "pnl_view": _stub_responder,
    "watchlist_view": _stub_responder,
    "unknown": _stub_responder,
}
