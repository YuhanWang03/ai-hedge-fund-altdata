"""Keyword fallback classifier — no LLM, no network."""

from __future__ import annotations

from app.runner.intent_adapter import _stub_classify


def test_classify_explain_move_chinese():
    name, args = _stub_classify("NVDA 为什么跌？")
    assert name == "explain_move"
    assert args.get("ticker") == "NVDA"


def test_classify_summary():
    name, args = _stub_classify("看看 AAPL 怎么样")
    assert name == "summary"
    assert args.get("ticker") == "AAPL"


def test_classify_chain():
    name, args = _stub_classify("找一下 AMD 的产业链")
    assert name == "chain"
    assert args.get("ticker") == "AMD"


def test_classify_thirteen_f():
    name, _ = _stub_classify("巴菲特最近买了什么")
    assert name == "thirteen_f"


def test_classify_holders():
    name, args = _stub_classify("谁持有 NVDA")
    assert name == "holders_view"
    assert args.get("ticker") == "NVDA"


def test_classify_find_anomalies():
    name, _ = _stub_classify("最近有什么异动")
    assert name == "find_anomalies"


def test_classify_unknown_fallback():
    name, _ = _stub_classify("今天天气怎么样")
    # Falls into "summary" because of "怎么样" keyword; documents behavior.
    assert name in {"summary", "unknown"}


def test_no_ticker_means_empty_args():
    name, args = _stub_classify("最近有什么异动")
    assert name == "find_anomalies"
    assert args == {}


# ---------------------------------------------------------------------------
# Phase 1 Stage 4 — earnings intents
# ---------------------------------------------------------------------------

def test_classify_earnings_view_with_ticker():
    name, args = _stub_classify("AAPL 什么时候发财报")
    assert name == "earnings_view"
    assert args.get("ticker") == "AAPL"


def test_classify_earnings_view_chinese_company_no_ticker():
    """Chinese company name without ticker → still routes to earnings_view,
    even though the ticker extractor can't map 苹果 → AAPL on its own.
    The responder will return a friendly error in that case."""
    name, _ = _stub_classify("苹果 财报")
    assert name == "earnings_view"


def test_classify_earnings_calendar_next_week():
    name, args = _stub_classify("下周谁要发财报")
    assert name == "earnings_calendar"
    assert args.get("days_horizon") == 7


def test_classify_earnings_calendar_default_horizon():
    """Phrase 'earnings calendar' without an explicit horizon → no
    days_horizon arg, so responder applies its default (14)."""
    name, args = _stub_classify("看看 earnings calendar")
    assert name == "earnings_calendar"
    assert "days_horizon" not in args


def test_classify_earnings_calendar_next_month():
    name, args = _stub_classify("下个月 财报安排")
    # "下个月" + "财报安排" — both phrases route to calendar; days=30 from
    # 下个月 phrase.
    assert name == "earnings_calendar"
    assert args.get("days_horizon") == 30


def test_existing_intents_not_regressed_by_earnings():
    """Adding earnings keywords must NOT swallow the existing 15."""
    cases = [
        ("NVDA 为什么跌？", "explain_move"),
        ("看看 AAPL 怎么样", "summary"),
        ("找一下 AMD 的产业链", "chain"),
        ("巴菲特最近买了什么", "thirteen_f"),
        ("谁持有 NVDA", "holders_view"),
        ("ark 今天买啥", "etf_view"),
        ("最近有什么异动", "find_anomalies"),
        ("提醒我 NVDA 突破 130", "alert_set"),
        ("我的 portfolio", "portfolio_view"),
        ("今日盈亏", "pnl_view"),
        ("watchlist", "watchlist_view"),
        ("设置", "settings"),
    ]
    for text, expected in cases:
        name, _ = _stub_classify(text)
        assert name == expected, f"{text!r} → {name} (want {expected})"
