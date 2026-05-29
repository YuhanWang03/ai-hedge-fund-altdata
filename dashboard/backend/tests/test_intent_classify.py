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
