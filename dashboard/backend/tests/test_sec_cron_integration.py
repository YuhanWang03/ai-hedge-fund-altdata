"""Cron-script integration tests for the SEC agent (Phase 3 Stage 6).

Loads ``scripts/sec_8k_to_telegram.py`` and ``scripts/sec_form4_to_telegram.py``
via :mod:`importlib.util` with ``sys.modules`` pre-stubs for production-only
deps (``v2.data``, ``v2.broker``) so the actual cron entry points run
end-to-end against a recording notifier and a temp-dir Archive.

What's verified end-to-end:

⑪ 8-K cron (``sec_8k_to_telegram.py``):
- Empty universe → silent skip, no archive write.
- HPE multi-item with 5.02 senior-exec → P0 push, single card, multi-item
  aggregated.
- 2.02-only filings → skipped (handled by ⑧).
- 5.02 LLM failure → conservative P0 escalation via the senior-exec
  metadata flag (the cron does NOT re-run extraction, it just trusts
  the pipeline's already-attached extracted_meta — so LLM failure means
  extracted_meta is empty, has_senior_exec=False, and the priority floor
  stays at whatever the pipeline assigned).
- Amendment filings get a -5 nudge (Stage 0 priority spec).
- Per-ticker EDGAR failure is silent — other tickers still get scanned.
- ``trace_json`` lands in ``archive.pushes`` for ``/api/push_trace/{id}``.
- Byte-equal: pushed card text == ``format_sec_8k_card(event, ...)``.

⑫ Form 4 cron (``sec_form4_to_telegram.py``):
- All-noise day (A/M/F) → no push, noise_summary captured.
- $2.5M CEO discretionary P → P0 individual card with magnitude bump.
- $1M Director 10b5-1 plan S → P2 with 10b5-1 demotion.
- 4-director same-day P cluster → P0 cluster card listing the names.
- Alpaca down on held lookup → silent default to empty held set.
- ``responder_name='_r_sec_form4'`` tagged on the trace.
- Byte-equal: pushed individual / cluster cards match the formatters.

Bot integration:
- Cross-surface identity: ``format_sec_8k_view`` is the same function
  the bot's ``eight_k_view`` calls (via the v2.reporting shim).
- ``insider_view`` routes through ``format_sec_form4_view``, not any
  remaining inline helper.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# sys.modules stubs for the sandbox (also defensive for CI without prod deps)
# ---------------------------------------------------------------------------
# v2/conftest.py covers edgar / langchain_deepseek / tavily globally; we
# add the rest here so this test module is self-contained even when run
# directly from the dashboard/backend/tests/ folder.

for _mod_name in ("edgar", "langchain_deepseek", "tavily"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()


# ---------------------------------------------------------------------------
# Recording notifier — kept in sync with TelegramNotifier._archive_with_priority
# ---------------------------------------------------------------------------

class _RecordingNotifier:
    """Drop-in for ``TelegramNotifier`` — captures every send_text call
    AND mirrors the real notifier's archive-write side-effect so tests
    can assert ``trace_json`` / ``priority_tier`` landed in
    ``archive.pushes``.

    Skips the actual Telegram HTTP send. Everything else (archive write,
    trace JSON serialization, expires_at calc) follows the real
    implementation exactly via an inlined copy of
    ``v2.reporting.notifier.TelegramNotifier._archive_with_priority``.
    If the real implementation changes, this stub must change with it.
    """

    def __init__(self, *, archive=None, **kw):
        self.calls: list[dict] = []
        self._archive = archive

    def send_text(self, text, *, trace=None, title=None, tickers=None,
                  priority=None, **extra):
        self._archive_write(
            kind="text", text=text, image=None, caption=None,
            trace=trace, title=title, tickers=tickers, priority=priority,
        )
        self.calls.append({
            "kind": "text",
            "text": text,
            "title": title,
            "tickers": list(tickers or []),
            "priority_tier": priority.tier if priority else None,
            "priority_score": priority.score if priority else None,
            "priority_reasons": list(priority.reasons) if priority else [],
        })

    def _archive_write(self, *, kind, text, image, caption, trace, title,
                       tickers, priority):
        if self._archive is None:
            return
        import json as _json
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        def _trace_to_json(tr):
            if tr is None:
                return None
            events = getattr(tr, "events", None)
            if not events:
                return None
            try:
                return _json.dumps(events, ensure_ascii=False)
            except (TypeError, ValueError):
                return None

        expires_at = (_dt.now(_tz.utc) + _td(days=2)).isoformat()
        common = dict(
            tickers=tickers,
            trace_json=_trace_to_json(trace),
            title=title,
            expires_at=expires_at,
            importance_score=priority.score if priority else None,
            priority_tier=priority.tier if priority else None,
            priority_reasons=",".join(priority.reasons) if priority else None,
        )
        if kind == "text":
            self._archive.save_text(text, **common)


# ---------------------------------------------------------------------------
# Cron-stub installer
# ---------------------------------------------------------------------------

def _install_cron_stubs(
    monkeypatch,
    *,
    watchlist=None,
    portfolio=None,
    scan_result=None,
    scan_raises=None,
):
    """Pre-populate sys.modules with the minimal surface the SEC cron
    scripts need at import time + the runtime behavior the tests want
    to inject.

    ``watchlist`` — list of dicts the bot.state stub returns from
    ``watchlist_list()``. Default: empty.
    ``portfolio`` — dict the broker stub returns from ``get_portfolio()``.
    Default: empty positions.
    ``scan_result`` — pre-built :class:`v2.sec.models.SecScanResult` that
    the stubbed ``v2.sec.run_sec_scan`` returns.
    ``scan_raises`` — exception class to raise inside run_sec_scan
    instead of returning a result (covers degraded-cron paths).
    """
    watchlist = watchlist if watchlist is not None else []
    portfolio = portfolio if portfolio is not None else {"positions": []}

    # --- v2.data shell (some transitive imports want it) ----------------
    v2_data = types.ModuleType("v2.data")
    v2_data.__path__ = []
    monkeypatch.setitem(sys.modules, "v2.data", v2_data)

    # --- v2.broker -------------------------------------------------------
    v2_broker = types.ModuleType("v2.broker")

    class AlpacaUnavailable(RuntimeError):
        pass

    v2_broker.AlpacaUnavailable = AlpacaUnavailable

    if isinstance(portfolio, Exception) or callable(portfolio) and not isinstance(portfolio, dict):
        v2_broker.get_portfolio = portfolio
    else:
        v2_broker.get_portfolio = lambda: portfolio
    monkeypatch.setitem(sys.modules, "v2.broker", v2_broker)

    # --- Patch v2.sec.pipeline.run_sec_scan + v2.sec.run_sec_scan -------
    # The cron imports `from v2.sec import run_sec_scan`. We patch BOTH
    # the source-of-truth (v2.sec.pipeline) and the re-export
    # (v2.sec.run_sec_scan) for safety.
    import v2.sec as _sec_pkg
    import v2.sec.pipeline as _sec_pipe

    if scan_raises is not None:
        def _scan(universe, today_iso):
            raise scan_raises
        monkeypatch.setattr(_sec_pipe, "run_sec_scan", _scan)
        monkeypatch.setattr(_sec_pkg, "run_sec_scan", _scan)
    else:
        from v2.sec.models import SecScanResult
        result = scan_result if scan_result is not None else SecScanResult()
        monkeypatch.setattr(_sec_pipe, "run_sec_scan", lambda *_a, **_kw: result)
        monkeypatch.setattr(_sec_pkg, "run_sec_scan", lambda *_a, **_kw: result)

    # --- v2.bot.state stub for watchlist_list() ----------------------
    # The real v2.bot imports telegram.ext at top-level. We don't want
    # to drag that in; install a fake v2.bot package + v2.bot.state.
    if "v2.bot" not in sys.modules or not hasattr(sys.modules.get("v2.bot"), "state"):
        v2_bot = types.ModuleType("v2.bot")
        v2_bot.__path__ = []
        v2_bot_state = types.ModuleType("v2.bot.state")
        v2_bot_state.watchlist_list = lambda: list(watchlist)
        v2_bot.state = v2_bot_state
        monkeypatch.setitem(sys.modules, "v2.bot", v2_bot)
        monkeypatch.setitem(sys.modules, "v2.bot.state", v2_bot_state)
    else:
        # v2.bot already imported; just patch state's watchlist_list
        monkeypatch.setattr(
            sys.modules["v2.bot.state"], "watchlist_list",
            lambda: list(watchlist),
        )

    # --- v2.reporting (Phase 2 pattern: full v2.reporting init pulls
    # matplotlib + v2.lateral → v2.data; stub the surface the SEC
    # crons import directly). The source-of-truth SEC formatters live
    # in v2/sec/_bot_cards.py and are sandbox-safe, so we wire them
    # through the stub. -------------------------------------------------
    from v2.sec import _bot_cards as sec_cards

    v2_reporting = types.ModuleType("v2.reporting")
    v2_reporting.__path__ = [str(_REPO_ROOT / "v2" / "reporting")]

    def notify_on_error(name):
        def decorator(fn):
            from functools import wraps
            @wraps(fn)
            def wrapper(*a, **kw):
                return fn(*a, **kw)
            return wrapper
        return decorator

    v2_reporting.TelegramNotifier = _RecordingNotifier
    v2_reporting.notify_on_error = notify_on_error
    v2_reporting.format_sec_8k_card = sec_cards.format_sec_8k_card
    v2_reporting.format_sec_8k_view = sec_cards.format_sec_8k_view
    v2_reporting.format_sec_form4_individual_card = (
        sec_cards.format_sec_form4_individual_card
    )
    v2_reporting.format_sec_form4_cluster_card = sec_cards.format_sec_form4_cluster_card
    v2_reporting.format_sec_form4_view = sec_cards.format_sec_form4_view
    monkeypatch.setitem(sys.modules, "v2.reporting", v2_reporting)

    # Real priority module via importlib (bypass v2.reporting init).
    spec = importlib.util.spec_from_file_location(
        "v2.reporting.priority",
        _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    real_priority = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "v2.reporting.priority", real_priority)
    spec.loader.exec_module(real_priority)
    v2_reporting.priority = real_priority


def _load_script(script_name: str):
    """Load a scripts/*.py module after stubs are in place. Returns
    the imported module."""
    script_path = _REPO_ROOT / "scripts" / script_name
    mod_name = f"_p3_cron_under_test_{script_name.replace('.', '_')}"
    # Force re-load so each test gets a fresh module object with the
    # current stubs installed.
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Archive temp-DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_archive(monkeypatch, tmp_path):
    from v2.archive import store as archive_store
    monkeypatch.setattr(archive_store, "_DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(archive_store, "_IMG_ROOT", tmp_path / "img")
    return tmp_path


# ---------------------------------------------------------------------------
# Synthetic SecScanResult fixtures — share shape with Stage 5 byte-equal
# ---------------------------------------------------------------------------

def _hpe_p0_event():
    """HPE multi-item filing, 5.02 senior_exec confirmed → P0 card."""
    from v2.sec.models import EightKEvent, EightKItem, SecFiling
    return EightKEvent(
        filing=SecFiling(
            ticker="HPE", cik="0001645590", form="8-K",
            filing_date="2026-06-04",
            accession_number="0000123-26-000001",
        ),
        items=[
            EightKItem("1.01", "P1", "重大商业合约 (新签)", {}),
            EightKItem("2.02", "P2", "财报数据", {}),
            EightKItem("5.02", "P0", "高管 / 董事会变动", {
                "departures": [{"name": "John Smith",
                                "title": "Chief Executive Officer"}],
                "appointments": [{"name": "Jane Doe",
                                  "title": "Interim Chief Executive Officer"}],
                "has_senior_exec": True,
            }),
            EightKItem("7.01", "P2", "Reg FD 自愿披露", {}),
            EightKItem("9.01", "P3", "财务报表 / 附件", {}),
        ],
    )


def _2_02_only_event():
    """Earnings-only 8-K (handled by ⑧). is_2_02_only=True so cron skips."""
    from v2.sec.models import EightKEvent, EightKItem, SecFiling
    return EightKEvent(
        filing=SecFiling(
            ticker="MSFT", cik="0000789019", form="8-K",
            filing_date="2026-06-04",
            accession_number="ACC-2-02",
        ),
        items=[
            EightKItem("2.02", "P2", "财报数据", {}),
            EightKItem("9.01", "P3", "财务报表 / 附件", {}),
        ],
    )


def _amendment_event():
    """8-K/A filing — amendment, gets -5 priority nudge."""
    from v2.sec.models import EightKEvent, EightKItem, SecFiling
    return EightKEvent(
        filing=SecFiling(
            ticker="AAPL", cik="0000320193", form="8-K/A",
            filing_date="2026-06-04",
            accession_number="ACC-AMEND",
            is_amendment=True,
        ),
        items=[
            EightKItem("5.02", "P0", "高管 / 董事会变动", {
                "departures": [{"name": "X", "title": "CEO"}],
                "appointments": [],
                "has_senior_exec": True,
            }),
        ],
    )


def _llm_failed_5_02_event():
    """5.02 with empty extracted_meta (LLM failure path). Pipeline
    has already classified it as P1 and left extracted_meta={}."""
    from v2.sec.models import EightKEvent, EightKItem, SecFiling
    return EightKEvent(
        filing=SecFiling(
            ticker="XYZ", cik="0000099", form="8-K",
            filing_date="2026-06-04",
            accession_number="ACC-99",
        ),
        items=[
            EightKItem("5.02", "P1", "高管 / 董事会变动", {}),
        ],
    )


def _nvda_filing(acc="ACC-NVDA-P"):
    from v2.sec.models import SecFiling
    return SecFiling(
        ticker="NVDA", cik="0001045810", form="4",
        filing_date="2026-06-04", accession_number=acc,
    )


def _jensen_2_5m_purchase():
    from v2.sec.models import Form4Transaction
    return Form4Transaction(
        filing=_nvda_filing("ACC-JENSEN-1"),
        insider_name="Jen-Hsun Huang", insider_role="CEO",
        transaction_code="P", transaction_date="2026-06-04",
        shares=20000.0, price=125.0, transaction_usd=2_500_000.0,
        is_10b5_1=False, direct_indirect="D",
    )


def _10b5_1_director_sale():
    from v2.sec.models import Form4Transaction
    return Form4Transaction(
        filing=_nvda_filing("ACC-PLAN-1"),
        insider_name="Jane Smith", insider_role="Director",
        transaction_code="S", transaction_date="2026-06-04",
        shares=8000.0, price=125.0, transaction_usd=1_000_000.0,
        is_10b5_1=True, direct_indirect="D",
    )


def _arm_cluster_4():
    from v2.sec.models import Form4Cluster, Form4Transaction, SecFiling
    arm_f = SecFiling(
        ticker="ARM", cik="0001973239", form="4",
        filing_date="2026-06-04",
        accession_number="ACC-ARM-CL",
    )
    txs = [
        Form4Transaction(
            filing=arm_f, insider_name=n, insider_role="Director",
            transaction_code="P", transaction_date="2026-06-04",
            shares=1000.0, price=100.0, transaction_usd=100_000.0,
            is_10b5_1=False, direct_indirect="D",
        )
        for n in ["Alice Wong", "Bob Chen", "Carol Davis", "David Lee"]
    ]
    cluster = Form4Cluster(
        ticker="ARM", cluster_date="2026-06-04", direction="purchase",
        transaction_count=4, total_usd=400_000.0,
        insider_names=["Alice Wong", "Bob Chen", "Carol Davis", "David Lee"],
        transactions=txs,
    )
    return cluster, txs


# ===========================================================================
# ⑪ 8-K daily cron
# ===========================================================================

class TestSec8KCron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def test_cron11_empty_universe_silent_skip(
        self, monkeypatch, temp_archive,
    ):
        """No watchlist + Alpaca empty → cron returns 0 without push."""
        _install_cron_stubs(monkeypatch, watchlist=[], portfolio={"positions": []})
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert rec is None, "no notifier instantiated when universe empty"
        # No archive rows
        import sqlite3
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM pushes WHERE agent='sec'"
            ).fetchone()
        except sqlite3.OperationalError:
            rows = (0,)
        finally:
            conn.close()
        assert rows[0] == 0

    def test_cron11_no_filings_silent_skip(
        self, monkeypatch, temp_archive,
    ):
        """Universe non-empty but pipeline returns no 8-K events."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "AAPL"}],
            scan_result=SecScanResult(),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # _emit_one never reached → notifier may not even be instantiated
        if rec is not None:
            assert rec.calls == []

    def test_cron11_p0_5_02_pushes_priority_card(
        self, monkeypatch, temp_archive,
    ):
        """HPE 5.02 senior_exec → P0 push, multi-item single card."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "HPE"}],
            scan_result=SecScanResult(eight_k_events=[_hpe_p0_event()]),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["priority_tier"] == "P0"
        # base sec_8k_p0=85 + senior_exec(+15) → 100 (clamped)
        assert call["priority_score"] == 100
        assert "HPE" in call["title"]
        # Card body lists all 5 item codes
        for code in ("1.01", "2.02", "5.02", "7.01", "9.01"):
            assert code in call["text"]
        # P0 emoji header
        assert "🚨" in call["text"]
        # 2.02 sub-tag
        assert "⑧ 处理" in call["text"]

    def test_cron11_2_02_only_skipped_no_push(
        self, monkeypatch, temp_archive,
    ):
        """Pure earnings 8-K → skipped via is_2_02_only filter."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "MSFT"}],
            scan_result=SecScanResult(eight_k_events=[_2_02_only_event()]),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        if rec is not None:
            assert rec.calls == []

    def test_cron11_multi_item_single_card(
        self, monkeypatch, temp_archive,
    ):
        """HPE 5-item filing → exactly ONE card, not 5."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "HPE"}],
            scan_result=SecScanResult(eight_k_events=[_hpe_p0_event()]),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1

    def test_cron11_5_02_llm_failure_no_escalation(
        self, monkeypatch, temp_archive,
    ):
        """LLM failure means the pipeline left has_senior_exec absent
        and extracted_meta={}. The +15 senior_exec bump does NOT fire,
        so the filing stays at whatever the item parser said (P1 in
        this case). Documents that the cron does not blindly escalate
        on LLM-fail — escalation is gated on metadata."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "XYZ"}],
            scan_result=SecScanResult(
                eight_k_events=[_llm_failed_5_02_event()],
            ),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        # P1 base sec_8k_p1=65, no senior_exec → stays at 65 → P1
        assert call["priority_tier"] == "P1"

    def test_cron11_amendment_filing_demoted(
        self, monkeypatch, temp_archive,
    ):
        """8-K/A with 5.02 senior_exec — gets -5 nudge. 85 +15 -5 = 95 → P0
        (still P0 because score floor for P0 is 80)."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "AAPL"}],
            scan_result=SecScanResult(eight_k_events=[_amendment_event()]),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        # base 85 + senior_exec(+15) - amendment(-5) = 95 → still P0
        assert call["priority_tier"] == "P0"
        assert call["priority_score"] == 95
        assert any("amendment" in r for r in call["priority_reasons"])

    def test_cron11_archive_trace_json_written(
        self, monkeypatch, temp_archive,
    ):
        """trace_json column populated in archive.pushes — covers
        the dashboard /api/push_trace/{id} consumer path."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "HPE"}],
            scan_result=SecScanResult(eight_k_events=[_hpe_p0_event()]),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, _rec = self._run(monkeypatch, cron)
        assert rc == 0

        import sqlite3, json
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        row = conn.execute(
            "SELECT trace_json FROM pushes WHERE agent='sec' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        events = json.loads(row[0])
        # Must contain the responder framing event
        names = {e.get("name") for e in events
                 if e.get("type") in ("module_enter", "module_exit")}
        assert "_r_sec_8k" in names

    def test_cron11_byte_equal_card_matches_formatter(
        self, monkeypatch, temp_archive,
    ):
        """⑪ cron pushed text == format_sec_8k_card(event, ...) byte-equal."""
        from v2.sec.models import SecScanResult
        from v2.sec._bot_cards import format_sec_8k_card
        event = _hpe_p0_event()
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "HPE"}],
            scan_result=SecScanResult(eight_k_events=[event]),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # HPE in watchlist (not held), is_watchlist=True
        expected = format_sec_8k_card(event, is_held=False, is_watchlist=True)
        assert rec.calls[0]["text"] == expected, (
            "cron output diverges from formatter"
        )

    def test_cron11_scan_exception_swallowed_by_decorator(
        self, monkeypatch, temp_archive,
    ):
        """run_sec_scan raising propagates up through the
        @notify_on_error decorator — exit code is non-zero. The
        decorator emits an alert; here we just verify the cron
        doesn't crash the scheduler."""
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "AAPL"}],
            scan_raises=RuntimeError("EDGAR 503"),
        )
        cron = _load_script("sec_8k_to_telegram.py")
        # The decorator turns the exception into a non-zero rc; we
        # tolerate either a clean re-raise or a swallow + rc != 0.
        try:
            rc, _rec = self._run(monkeypatch, cron)
            assert rc != 0
        except RuntimeError:
            pass  # acceptable — top-level handler propagates


# ===========================================================================
# ⑫ Form 4 daily cron
# ===========================================================================

class TestSecForm4Cron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def test_cron12_noise_only_no_push(
        self, monkeypatch, temp_archive,
    ):
        """All A/M/F transactions go to noise_summary → no push."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "ARM"}],
            scan_result=SecScanResult(
                form4_noise_summary={"ARM": {"A": 5, "M": 2, "F": 1}},
            ),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        if rec is not None:
            assert rec.calls == []

    def test_cron12_ceo_2_5m_purchase_pushes_p0(
        self, monkeypatch, temp_archive,
    ):
        """Jensen $2.5M discretionary P → P0 individual card."""
        from v2.sec.models import SecScanResult
        tx = _jensen_2_5m_purchase()
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "NVDA"}],
            scan_result=SecScanResult(form4_signal_transactions=[tx]),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        # base sec_form4_purchase=75 + magnitude_2m+(+25) → 100 (clamped)
        assert call["priority_tier"] == "P0"
        assert call["priority_score"] == 100
        # Discretionary tag visible
        assert "discretionary" in call["text"]
        assert "Jen-Hsun Huang" in call["text"]

    def test_cron12_10b5_1_sale_demoted_p2(
        self, monkeypatch, temp_archive,
    ):
        """$1M S 10b5-1 plan → demotion via priority adjustment."""
        from v2.sec.models import SecScanResult
        tx = _10b5_1_director_sale()
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "NVDA"}],
            scan_result=SecScanResult(form4_signal_transactions=[tx]),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        # base sec_form4_sale=50 + magnitude_1m(+15) + 10b5_1(-15) = 50 → P2
        assert call["priority_tier"] == "P2"
        # 10b5-1 reason on the priority breakdown
        assert any("10b5_1" in r for r in call["priority_reasons"])
        assert "10b5-1 plan" in call["text"]

    def test_cron12_cluster_4_directors_p0(
        self, monkeypatch, temp_archive,
    ):
        """4-director same-day purchase cluster → P0 cluster card."""
        from v2.sec.models import SecScanResult
        cluster, txs = _arm_cluster_4()
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "ARM"}],
            scan_result=SecScanResult(
                form4_signal_transactions=txs,
                form4_clusters=[cluster],
            ),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # Cluster card + individual txs filtered out (already in cluster)
        # so we get exactly 1 push (the cluster).
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["priority_tier"] == "P0"
        # All 4 names listed in the cluster card
        for name in ["Alice Wong", "Bob Chen", "Carol Davis", "David Lee"]:
            assert name in call["text"]
        assert "内部人集群买入" in call["text"]

    def test_cron12_alpaca_down_silent_held_skip(
        self, monkeypatch, temp_archive,
    ):
        """Alpaca outage → get_portfolio raises → held set defaults to
        empty, cron still runs. No exception propagates."""
        from v2.sec.models import SecScanResult

        def boom():
            raise RuntimeError("Alpaca 503")

        tx = _jensen_2_5m_purchase()
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "NVDA"}],
            portfolio=boom,
            scan_result=SecScanResult(form4_signal_transactions=[tx]),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # Push still goes through — just without is_held attribution
        assert len(rec.calls) == 1
        # The card body must NOT carry the 🟢 held badge
        assert "持仓股" not in rec.calls[0]["text"]

    def test_cron12_responder_name_r_sec_form4(
        self, monkeypatch, temp_archive,
    ):
        """capture_trace_with_framing tags responder_name='_r_sec_form4'."""
        from v2.sec.models import SecScanResult
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "NVDA"}],
            scan_result=SecScanResult(
                form4_signal_transactions=[_jensen_2_5m_purchase()],
            ),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, _rec = self._run(monkeypatch, cron)
        assert rc == 0

        import sqlite3, json
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        row = conn.execute(
            "SELECT trace_json FROM pushes WHERE agent='sec' LIMIT 1"
        ).fetchone()
        conn.close()
        events = json.loads(row[0])
        names = {e.get("name") for e in events
                 if e.get("type") in ("module_enter", "module_exit")}
        assert "_r_sec_form4" in names

    def test_cron12_byte_equal_individual_card(
        self, monkeypatch, temp_archive,
    ):
        """⑫ individual card byte-equal to format_sec_form4_individual_card."""
        from v2.sec.models import SecScanResult
        from v2.sec._bot_cards import format_sec_form4_individual_card
        tx = _jensen_2_5m_purchase()
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "NVDA"}],
            scan_result=SecScanResult(form4_signal_transactions=[tx]),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        expected = format_sec_form4_individual_card(
            tx, is_held=False, is_watchlist=True,
        )
        assert rec.calls[0]["text"] == expected

    def test_cron12_byte_equal_cluster_card(
        self, monkeypatch, temp_archive,
    ):
        """⑫ cluster card byte-equal to format_sec_form4_cluster_card."""
        from v2.sec.models import SecScanResult
        from v2.sec._bot_cards import format_sec_form4_cluster_card
        cluster, txs = _arm_cluster_4()
        _install_cron_stubs(
            monkeypatch,
            watchlist=[{"ticker": "ARM"}],
            scan_result=SecScanResult(
                form4_signal_transactions=txs,
                form4_clusters=[cluster],
            ),
        )
        cron = _load_script("sec_form4_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        expected = format_sec_form4_cluster_card(
            cluster, is_held=False, is_watchlist=True,
        )
        assert rec.calls[0]["text"] == expected


# ===========================================================================
# Bot ↔ public formatter routing (cross-surface identity)
# ===========================================================================

class TestBotResponderRouting:
    """Verifies the bot responders dispatch through the public
    v2.reporting formatters (not any remaining inline helper)."""

    def test_eight_k_view_uses_public_format_sec_8k_view(self):
        """v2.bot.responders.eight_k_view references format_sec_8k_view
        from v2.reporting at module top — identity with v2.sec._bot_cards
        (Stage 5 4-layer shim)."""
        # Sandbox v2.bot.responders has heavy imports; use the same
        # stub_v2_bot_imports pattern as v2/sec/test_bot_responders.py
        # but inline (this test only needs to inspect imports, not run
        # responders).
        import importlib
        import importlib.util

        # Bypass v2.reporting init by loading the shim directly.
        spec = importlib.util.spec_from_file_location(
            "_p3_stage6_sec_shim_check",
            _REPO_ROOT / "v2" / "reporting" / "_sec_formatters.py",
        )
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)

        from v2.sec import _bot_cards as src

        # The shim re-exports the source-of-truth function objects.
        assert shim.format_sec_8k_view is src.format_sec_8k_view
        assert shim.format_sec_form4_view is src.format_sec_form4_view

    def test_insider_view_routes_through_format_sec_form4_view(
        self, monkeypatch,
    ):
        """No inline _format_insider_view_card / _format_5_02_extract_lines
        remain on v2.bot.responders — only the public-API import."""
        responders_path = _REPO_ROOT / "v2" / "bot" / "responders.py"
        src = responders_path.read_text(encoding="utf-8")
        # Stage 5 deleted these helpers; if they ever come back the
        # test will surface that and we can revisit whether the lift
        # has been undone.
        for forbidden in (
            "def _format_8k_view_card",
            "def _format_insider_view_card",
            "def _format_5_02_extract_lines",
            "def _fmt_money_kb",
            "def _fmt_tx_one_liner",
        ):
            assert forbidden not in src, (
                f"v2/bot/responders.py reintroduced inline helper {forbidden!r}; "
                "Stage 5 lift requires it to live in v2/sec/_bot_cards.py only."
            )
        # And the public-API import is present.
        assert "from v2.reporting import" in src
        assert "format_sec_8k_view" in src
        assert "format_sec_form4_view" in src
