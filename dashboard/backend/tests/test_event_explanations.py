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
    # remember replaces the older anomaly_memory_remember key — actual __name__
    # of v2.memory.AnomalyMemory.remember is "remember".
    for fn in ("save_filing", "remember", "archive_push"):
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


# ---------------------------------------------------------------------------
# Tier 1 catalogue expansion — coverage for the 14 responder modules,
# Alpaca / ARK CSV / new LLM roles / db_read tables / validate kinds /
# 10 new render cards / 3 new db_write functions.
# ---------------------------------------------------------------------------

def test_all_14_responder_modules_have_entries():
    expected = {
        "_r_thirteen_f", "_r_explain_move",
        "_r_chain", "_r_summary",
        "_r_holders_view", "_r_etf_view",
        "_r_portfolio_view", "_r_pnl_view",
        "_r_alert_set", "_r_alert_list", "_r_alert_remove",
        "_r_watchlist_view", "_r_watchlist_add", "_r_watchlist_remove",
        "_r_settings", "_r_find_anomalies",
    }
    for name in expected:
        e = lookup({"type": "module_enter", "name": name})
        assert e is not None, f"missing module explanation for {name}"
        assert all(e[k] for k in ("source", "how", "what", "store", "next"))


def test_new_api_providers():
    assert lookup({"type": "api_call", "provider": "ark_csv", "endpoint": "fetch_holdings"}) is not None
    assert lookup({"type": "api_call", "provider": "alpaca",  "endpoint": "get_account"}) is not None
    assert lookup({"type": "api_call", "provider": "alpaca",  "endpoint": "get_all_positions"}) is not None


def test_llm_role_proposer_fingerprint():
    e = lookup({
        "type": "llm_call",
        "prompt_preview": "你是一名资深科技股研究分析师。给定一组种子股票...",
    })
    assert e is not None
    assert "邻居" in e["next"] or "Tavily" in e["next"]


def test_llm_role_narrator_fingerprint():
    e = lookup({
        "type": "llm_call",
        "prompt_preview": "你是一名资深科技股分析师。对每只股票给出 bull + bear...",
    })
    assert e is not None
    assert "/summary" in e["next"] or "summary" in e["next"].lower()


def test_new_transform_ops():
    assert lookup({"type": "transform", "op": "etf_diff"}) is not None
    assert lookup({"type": "transform", "op": "filter"}) is not None


def test_db_read_resolves_via_db_label():
    for db in ("edgar.db", "etf.db", "bot_state.db", "archive.db"):
        e = lookup({"type": "db_read", "db": db})
        assert e is not None, f"missing db_read explanation for {db}"


def test_db_read_resolves_via_table_fallback():
    # If the emit site forgot the db field but supplied a known table key,
    # the lookup still finds the right entry.
    e = lookup({"type": "db_read", "table": "bot_state.db"})
    assert e is not None


def test_validate_kinds():
    assert lookup({"type": "validate", "what": "ticker"}) is not None
    assert lookup({"type": "validate", "what": "price"}) is not None


def test_new_render_cards():
    expected = {
        "lateral_result", "summary_card", "holders_card", "etf_snapshot",
        "portfolio_card", "pnl_card", "alerts_list",
        "watchlist_card", "anomalies_list", "settings_card",
    }
    for card in expected:
        e = lookup({"type": "render", "card": card})
        assert e is not None, f"missing render explanation for {card}"


def test_new_db_writes():
    for fn in ("save_snapshot", "alert_add", "alert_remove",
               "watchlist_add", "watchlist_remove"):
        e = lookup({"type": "db_write", "fn": fn})
        assert e is not None, f"missing db_write explanation for {fn}"
