"""Tests for the observability SDK.

Designed to run in any environment — no v2/data, no API keys, no network.
"""

from __future__ import annotations

import threading

from v2.observability import emit, install_all, installed_hooks
from v2.observability.hooks import _wrap_fd_method, _wrap_llm_invoke
from v2.observability.pricing import (
    deepseek_cost,
    estimate_cost,
    tavily_cost,
)
from v2.observability.trace import TRACE_CTX, Trace, current_trace


def _collect_sink() -> tuple[list[dict], callable]:
    events: list[dict] = []
    lock = threading.Lock()

    def sink(ev: dict) -> None:
        with lock:
            events.append(ev)

    return events, sink


def test_emit_outside_trace_is_noop() -> None:
    # No trace bound — must not raise, must not record anything.
    assert current_trace() is None
    emit("anything", foo="bar")


def test_trace_emit_assigns_monotonic_seq() -> None:
    events, sink = _collect_sink()
    trace = Trace(session_id="sess_test", sink=sink)
    trace.emit("a", x=1)
    trace.emit("b", x=2)
    trace.emit("c", x=3)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["type"] for e in events] == ["a", "b", "c"]
    assert all(e["session_id"] == "sess_test" for e in events)


def test_emit_uses_contextvar_binding() -> None:
    events, sink = _collect_sink()
    trace = Trace(session_id="sess_ctx", sink=sink)
    token = TRACE_CTX.set(trace)
    try:
        emit("api_call", provider="fake", endpoint="x")
    finally:
        TRACE_CTX.reset(token)
    assert len(events) == 1
    assert events[0]["provider"] == "fake"
    # After reset, emit is a no-op again.
    emit("api_call", provider="should_not_appear")
    assert len(events) == 1


def test_sink_exception_does_not_break_caller() -> None:
    def bad_sink(ev: dict) -> None:
        raise RuntimeError("sink down")

    trace = Trace(session_id="sess_bad", sink=bad_sink)
    # Must not raise.
    trace.emit("a", x=1)


def test_fd_method_wrapper_emits_api_call() -> None:
    class FakeFD:
        def get_prices(self, ticker, days=5):
            return [ticker, days]

    # Wrap manually using the same builder the installer uses.
    original = FakeFD.get_prices
    FakeFD.get_prices = _wrap_fd_method("FakeFD.get_prices")(original)

    events, sink = _collect_sink()
    trace = Trace(session_id="sess_fd", sink=sink)
    token = TRACE_CTX.set(trace)
    try:
        out = FakeFD().get_prices("NVDA", days=30)
    finally:
        TRACE_CTX.reset(token)

    assert out == ["NVDA", 30]
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "api_call"
    assert ev["provider"] == "fd"
    assert ev["ticker"] == "NVDA"
    assert ev["endpoint"] == "FakeFD.get_prices"


def test_fd_wrapper_is_passthrough_without_trace() -> None:
    class FakeFD:
        def get_prices(self, ticker):
            return ticker.upper()

    FakeFD.get_prices = _wrap_fd_method("FakeFD.get_prices")(FakeFD.get_prices)
    # No trace bound — wrapper must just return the result, no emit.
    assert FakeFD().get_prices("nvda") == "NVDA"


def test_llm_wrapper_extracts_usage_metadata() -> None:
    class FakeMessage:
        def __init__(self, content):
            self.content = content
            self.usage_metadata = {"input_tokens": 1200, "output_tokens": 180}

    class FakeChat:
        model_name = "deepseek-chat"

        def invoke(self, messages, **kwargs):
            return FakeMessage("OK")

    FakeChat.invoke = _wrap_llm_invoke(FakeChat.invoke)

    events, sink = _collect_sink()
    trace = Trace(session_id="sess_llm", sink=sink)
    token = TRACE_CTX.set(trace)
    try:
        result = FakeChat().invoke("hello")
    finally:
        TRACE_CTX.reset(token)

    assert result.content == "OK"
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "llm_call"
    assert ev["input_tokens"] == 1200
    assert ev["output_tokens"] == 180
    assert ev["cost_usd"] > 0
    assert ev["model"] == "deepseek-chat"
    assert ev["prompt_preview"] == "hello"


def test_install_all_is_idempotent() -> None:
    # First call: returns whatever modules are present (may be empty in CI).
    first = install_all()
    second = install_all()
    # Re-running must not duplicate counts — the sentinel prevents
    # double-wrapping, so the second call's recorded hook list should be
    # empty (nothing newly patched).
    assert installed_hooks() == second
    # Returned hooks counts on the second call should all be zero entries
    # (i.e., the list should be empty because no NEW patches were applied).
    assert second == []
    # First call may or may not have entries depending on environment.
    _ = first


def test_pricing_table_covers_all_known_intents() -> None:
    # The known intents from the README's NL classifier.
    known = {
        "explain_move", "summary", "chain", "thirteen_f", "holders_view",
        "etf_view", "alert_set", "alert_list", "alert_remove",
        "portfolio_view", "pnl_view", "watchlist_view", "find_anomalies",
        "unknown",
    }
    for name in known:
        assert estimate_cost(name) > 0


def test_pricing_math() -> None:
    # 1M input tokens at $0.14 per 1M = $0.14
    assert abs(deepseek_cost(1_000_000, 0) - 0.14) < 1e-6
    # 1M output tokens at $0.28 per 1M = $0.28
    assert abs(deepseek_cost(0, 1_000_000) - 0.28) < 1e-6
    # One Tavily search.
    assert tavily_cost(1) == 0.005
    assert tavily_cost(10) == 0.05


def test_threading_safe_emit() -> None:
    events, sink = _collect_sink()
    trace = Trace(session_id="sess_thread", sink=sink)

    def worker():
        for i in range(100):
            trace.emit("tick", i=i)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(events) == 1000
    seqs = sorted(e["seq"] for e in events)
    # No duplicates, all assigned.
    assert seqs == list(range(1, 1001))
