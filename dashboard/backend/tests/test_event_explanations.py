"""Tests for the event explanation catalogue."""

from __future__ import annotations

from app.runner.event_explanations import lookup


def test_edgar_company_filings():
    ev = {"type": "api_call", "provider": "edgar", "endpoint": "Company.get_filings"}
    e = lookup(ev)
    assert e is not None
    assert "EDGAR" in e["source"]
    assert "HTTP" in e["how"]
    assert all(e[k] for k in ("source", "how", "what", "store", "next"))


def test_fd_endpoint_stripped_of_class_prefix():
    # The hook emits "CachedFDClient.get_prices"; lookup should resolve it.
    ev = {"type": "api_call", "provider": "fd", "endpoint": "CachedFDClient.get_prices"}
    e = lookup(ev)
    assert e is not None
    assert "financialdatasets" in e["source"]

    # Bare method name should also work (other callers).
    ev2 = {"type": "api_call", "provider": "fd", "endpoint": "get_prices"}
    assert lookup(ev2) == e


def test_tavily_search():
    ev = {"type": "api_call", "provider": "tavily", "endpoint": "search"}
    e = lookup(ev)
    assert e is not None
    assert "Tavily" in e["source"]
    assert "不持久化" in e["store"]


def test_transform_cusip_aggregate():
    ev = {"type": "transform", "op": "cusip_aggregate"}
    e = lookup(ev)
    assert e is not None
    assert "CUSIP" in e["how"]


def test_transform_detect_changes():
    ev = {"type": "transform", "op": "detect_changes"}
    e = lookup(ev)
    assert e is not None
    assert "DeepSeek" in e["next"]


def test_llm_role_intent_classifier():
    ev = {
        "type": "llm_call",
        "model": "deepseek-chat",
        "prompt_preview": "你是一个股票分析助手的【意图分类器】。\n把用户的话归类...",
    }
    e = lookup(ev)
    assert e is not None
    assert "意图" in e["next"] or "intent" in e["next"]


def test_llm_role_interpret_changes():
    ev = {
        "type": "llm_call",
        "model": "deepseek-chat",
        "prompt_preview": "你是一名机构持仓分析师。给定 manager...",
    }
    e = lookup(ev)
    assert e is not None
    assert "PositionChange" in e["next"]


def test_llm_role_generator():
    ev = {
        "type": "llm_call",
        "model": "deepseek-chat",
        "prompt_preview": "你是一名股票异动归因分析师。给定一次异动事件...",
    }
    e = lookup(ev)
    assert e is not None
    assert "归因" in e["what"] or "归因" in e["how"]


def test_llm_role_verifier():
    ev = {
        "type": "llm_call",
        "model": "deepseek-chat",
        "prompt_preview": "你是一名严苛的金融分析师，负责评估归因理由的因果链强度。",
    }
    e = lookup(ev)
    assert e is not None
    assert "Verifier" in e["next"] or "Generator" in e["next"]


def test_llm_unknown_prompt_returns_none():
    ev = {
        "type": "llm_call",
        "model": "deepseek-chat",
        "prompt_preview": "some future prompt nobody has seen",
    }
    assert lookup(ev) is None


def test_render_cards():
    for card in ("portfolio_snapshot", "institutional_summary",
                 "manager_detail", "anomaly_card"):
        e = lookup({"type": "render", "card": card})
        assert e is not None, f"missing render explanation for {card}"


def test_db_writes():
    for fn in ("save_filing", "anomaly_memory_remember", "archive_push"):
        e = lookup({"type": "db_write", "fn": fn})
        assert e is not None, f"missing db_write explanation for {fn}"


def test_module_enter_responder():
    for name in ("_r_thirteen_f", "_r_explain_move"):
        e = lookup({"type": "module_enter", "name": name})
        assert e is not None, f"missing module explanation for {name}"


def test_meta_intent_and_reply():
    for et in ("intent_classified", "chat_message"):
        e = lookup({"type": et})
        assert e is not None


def test_unknown_event_returns_none():
    # No-match cases must return None so the UI knows to skip the disclosure.
    assert lookup({"type": "session_start"}) is None
    assert lookup({"type": "session_end"}) is None
    assert lookup({"type": "module_exit", "name": "_r_thirteen_f"}) is None
    assert lookup({"type": "api_call", "provider": "weird", "endpoint": "x"}) is None
    assert lookup({"type": "transform", "op": "future_op"}) is None
