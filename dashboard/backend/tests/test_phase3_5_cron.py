"""Cron-script integration tests for Phase 3.5.

Two cron paths exercised end-to-end:

⑫b SEC Insider Digest (``scripts/sec_insider_digest_to_telegram.py``):
- Aggregates the past Mon-Fri's ⑫ Form 4 push titles from the
  ``archive.pushes`` table into a single weekly card.
- Quiet-week / unusual-week priority + tier flow verified through the
  same _RecordingNotifier mirror used by the Phase 3 cron tests.

⑧ Earnings Summaries × Phase 3.5 10-Q delta
(``scripts/earnings_summaries.py``):
- _fetch_recent_ten_q is monkey-patched module-level so the cron's call
  through run_summaries lands on the synthetic TenQDelta without
  reaching SEC EDGAR.
- Card body checked for the ``📋 10-Q MD&A 关键变化`` block.
- has_going_concern / has_material_weakness flags surface into the
  priority metadata (the Stage 4 cron fix in scripts/earnings_summaries.py
  forwards them — verified here).
- Silent-skip path tested: when _fetch_recent_ten_q raises, the earnings
  card still ships but the 10-Q section disappears.

Stub harness mirrors test_sec_cron_integration.py + test_earnings_cron_integration.py
patterns so a new sandbox import doesn't trip these tests up.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Defensive stubs (mirror Phase 3 / Phase 4 sandbox surface)
# ---------------------------------------------------------------------------

for _mod_name in ("edgar", "langchain_deepseek", "tavily", "fredapi"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()


# ---------------------------------------------------------------------------
# Recording notifier — mirrors TelegramNotifier._archive_with_priority
# ---------------------------------------------------------------------------

class _RecordingNotifier:
    """Drop-in for TelegramNotifier. Captures every send_text + writes
    the same archive row the real notifier would, so tests can assert
    importance_score / priority_tier / priority_reasons / trace_json
    landed in archive.pushes."""

    def __init__(self, *, archive=None, **kw):
        self.calls: list[dict] = []
        self._archive = archive

    def send_text(self, text, *, trace=None, title=None, tickers=None,
                  priority=None, **extra):
        self._archive_write(
            text=text, trace=trace, title=title,
            tickers=tickers, priority=priority,
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

    def _archive_write(self, *, text, trace, title, tickers, priority):
        if self._archive is None:
            return

        def _trace_to_json(tr):
            if tr is None:
                return None
            events = getattr(tr, "events", None)
            if not events:
                return None
            try:
                return json.dumps(events, ensure_ascii=False)
            except (TypeError, ValueError):
                return None

        expires_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self._archive.save_text(
            text,
            tickers=tickers,
            trace_json=_trace_to_json(trace),
            title=title,
            expires_at=expires_at,
            importance_score=priority.score if priority else None,
            priority_tier=priority.tier if priority else None,
            priority_reasons=",".join(priority.reasons) if priority else None,
        )


# ---------------------------------------------------------------------------
# Temp archive fixture (shared)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_archive(monkeypatch, tmp_path):
    from v2.archive import store as archive_store
    monkeypatch.setattr(archive_store, "_DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(archive_store, "_IMG_ROOT", tmp_path / "img")
    return tmp_path


# ---------------------------------------------------------------------------
# Script loader
# ---------------------------------------------------------------------------

def _load_script(script_name: str):
    script_path = _REPO_ROOT / "scripts" / script_name
    mod_name = f"_p3_5_cron_under_test_{script_name.replace('.', '_')}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# ⑫b SEC Insider Digest cron
# ===========================================================================

def _install_digest_cron_stubs(monkeypatch):
    """Minimal stub harness for the ⑫b cron — it only needs Archive +
    v2.observability + the v2.reporting re-exports (no broker / bot
    state — the digest reads its inputs from archive.pushes)."""
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
    v2_reporting.format_sec_insider_digest = sec_cards.format_sec_insider_digest
    monkeypatch.setitem(sys.modules, "v2.reporting", v2_reporting)

    spec = importlib.util.spec_from_file_location(
        "v2.reporting.priority",
        _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    real_priority = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "v2.reporting.priority", real_priority)
    spec.loader.exec_module(real_priority)
    v2_reporting.priority = real_priority


def _seed_form4_pushes(archive, week_start_iso: str, titles_with_dates: list[tuple[str, str]]):
    """Insert ⑫ Form 4 push rows with a controlled ts (ISO datetime).

    ``titles_with_dates`` is a list of ``(title, ts_iso)`` pairs. The
    digest cron's _query_form4_pushes compares ts lexically against the
    week window, so we pass full ISO datetimes."""
    with archive._conn() as conn:
        for title, ts in titles_with_dates:
            conn.execute(
                "INSERT INTO pushes (ts, agent, msg_type, text_html, title, "
                "tickers, importance_score, priority_tier) "
                "VALUES (?, 'sec', 'text', ?, ?, '', 70, 'P1')",
                (ts, f"<placeholder body for {title}>", title),
            )


def _current_week_window_iso() -> tuple[str, str]:
    """Mirror v2.sec.insider_digest.default_week_window for today."""
    from v2.sec.insider_digest import default_week_window
    return default_week_window(date.today().isoformat())


class TestInsiderDigestCron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def _seed_normal_week(self, temp_archive):
        """5 ⑫ pushes across 3 tickers, 1 cluster, no unusual activity."""
        from v2.archive import Archive
        archive = Archive("sec")
        week_start, _ = _current_week_window_iso()
        # ts in the middle of the week (Wed) so it always falls inside
        # whether today is Mon or Fri.
        wed = (date.fromisoformat(week_start) + timedelta(days=2)).isoformat() + "T15:00:00+00:00"
        _seed_form4_pushes(archive, week_start, [
            ("Form 4 · NVDA · 买入", wed),
            ("Form 4 · NVDA · 卖出", wed),
            ("Form 4 · AAPL · 买入", wed),
            ("Form 4 · MSFT · 卖出", wed),
            ("Form 4 集群 · ARM · purchase", wed),
        ])

    def test_fri_19_15_aggregates_week(self, monkeypatch, temp_archive):
        """5 ⑫ pushes across 3 tickers + 1 cluster → P2 digest card."""
        self._seed_normal_week(temp_archive)
        _install_digest_cron_stubs(monkeypatch)

        cron = _load_script("sec_insider_digest_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        assert rec is not None
        assert len(rec.calls) == 1, f"expected 1 push, got {len(rec.calls)}"
        call = rec.calls[0]
        # P2 floor (no unusual tickers — each ticker has ≤2 pushes).
        # base sec_insider_digest=55 → P2.
        assert call["priority_tier"] == "P2"
        assert call["priority_score"] == 55
        # Card body shows the digest header + 总览 + 方向分布.
        assert "内部人活动周报" in call["text"]
        assert "<b>本周总览</b>" in call["text"]
        # 5 pushes total (2 NVDA + 1 AAPL + 1 MSFT + 1 ARM cluster)
        assert "<code>5</code>" in call["text"]
        # 3 ticker counts (NVDA, AAPL, MSFT) — ARM cluster also counts → 4
        assert "<code>4</code> 只" in call["text"]

    def test_empty_week_silent_p2(self, monkeypatch, temp_archive):
        """No ⑫ pushes this week → '本周 ⑫ Form 4 推送平静' P2 card."""
        from v2.archive import Archive
        Archive("sec")   # ensures schema exists; no rows seeded
        _install_digest_cron_stubs(monkeypatch)

        cron = _load_script("sec_insider_digest_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        assert len(rec.calls) == 1, "operator-visibility floor — always pushes"
        call = rec.calls[0]
        assert call["priority_tier"] == "P2"
        assert call["priority_score"] == 55
        assert "本周 ⑫ Form 4 推送平静（0 笔）" in call["text"]
        # Polish 2 — quiet week omits footer caption
        assert "Phase 3.5 简化口径" not in call["text"]
        # No unusual ticker block on a quiet week
        assert "异常活跃 ticker" not in call["text"]

    def test_unusual_3_tickers_p1(self, monkeypatch, temp_archive):
        """3 tickers each ≥3 pushes → ⚠️ block + P1 upgrade + reason."""
        from v2.archive import Archive
        archive = Archive("sec")
        week_start, _ = _current_week_window_iso()
        wed = (date.fromisoformat(week_start) + timedelta(days=2)).isoformat() + "T15:00:00+00:00"

        rows: list[tuple[str, str]] = []
        # NVDA: 4 pushes (3 individual + 1 cluster)
        for _ in range(3):
            rows.append(("Form 4 · NVDA · 买入", wed))
        rows.append(("Form 4 集群 · NVDA · purchase", wed))
        # ARM: 3 individual pushes
        for _ in range(3):
            rows.append(("Form 4 · ARM · 买入", wed))
        # TSLA: 3 mixed pushes
        rows.append(("Form 4 · TSLA · 买入", wed))
        rows.append(("Form 4 · TSLA · 卖出", wed))
        rows.append(("Form 4 集群 · TSLA · sale", wed))
        _seed_form4_pushes(archive, week_start, rows)

        _install_digest_cron_stubs(monkeypatch)

        cron = _load_script("sec_insider_digest_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        call = rec.calls[0]
        # base 55 + unusual_tickers_3 +10 = 65 → P1
        assert call["priority_tier"] == "P1"
        assert call["priority_score"] == 65
        assert any("unusual_tickers_3" in r for r in call["priority_reasons"])
        # Body lists all 3 unusual tickers
        assert "异常活跃 ticker" in call["text"]
        for t in ("NVDA", "ARM", "TSLA"):
            assert f"<b>{t}</b>" in call["text"]
        # Tickers used in tickers field for downstream filtering
        assert set(call["tickers"]) == {"NVDA", "ARM", "TSLA"}

    def test_responder_name_correct(self, monkeypatch, temp_archive):
        """capture_trace_with_framing(responder_name='_r_sec_insider_digest')
        — verified via the trace_json written to archive.pushes."""
        self._seed_normal_week(temp_archive)
        _install_digest_cron_stubs(monkeypatch)

        cron = _load_script("sec_insider_digest_to_telegram.py")
        rc, _rec = self._run(monkeypatch, cron)
        assert rc == 0

        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        row = conn.execute(
            "SELECT trace_json FROM pushes "
            "WHERE agent='sec' AND title LIKE 'SEC 内部人周报%' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None and row[0]
        events = json.loads(row[0])
        names = {
            e.get("name") for e in events
            if e.get("type") in ("module_enter", "module_exit")
        }
        assert "_r_sec_insider_digest" in names, (
            f"responder_name missing from trace: {names}"
        )

    def test_byte_equal_card_matches_formatter(self, monkeypatch, temp_archive):
        """⑫b cron pushed card body == format_sec_insider_digest(digest)
        byte-equal — same source-of-truth route as Phase 3 cron tests."""
        self._seed_normal_week(temp_archive)
        _install_digest_cron_stubs(monkeypatch)

        cron = _load_script("sec_insider_digest_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0

        # Re-derive what the cron should have built — same code path
        # as the cron uses (build_weekly_digest + format_sec_insider_digest).
        from v2.archive import Archive
        from v2.sec._bot_cards import format_sec_insider_digest
        from v2.sec.insider_digest import build_weekly_digest, default_week_window

        archive = Archive("sec")
        ws, we = default_week_window(date.today().isoformat())
        digest = build_weekly_digest(archive, ws, we)
        expected = format_sec_insider_digest(digest)
        assert rec.calls[0]["text"] == expected, (
            f"\n--- actual ---\n{rec.calls[0]['text']}\n"
            f"\n--- expected ---\n{expected}"
        )


# ===========================================================================
# ⑧ Earnings × 10-Q integration
# ===========================================================================

def _install_earnings_cron_stubs(monkeypatch, *, alpaca_positions=None, watchlist=None):
    """Lift of the Phase 1 earnings stub harness — covers v2.data /
    v2.broker / v2.bot.state / v2.reporting + the priority module
    via importlib."""
    v2_data_pkg = types.ModuleType("v2.data")
    v2_data_pkg.__path__ = []
    v2_data_client = types.ModuleType("v2.data.client")

    class _FakeFD:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_earnings(self, t): return None
        def get_earnings_history(self, t, limit=4): return []

    v2_data_pkg.CachedFDClient = _FakeFD
    v2_data_pkg.FDClient = _FakeFD
    v2_data_client.FDClient = _FakeFD
    monkeypatch.setitem(sys.modules, "v2.data", v2_data_pkg)
    monkeypatch.setitem(sys.modules, "v2.data.client", v2_data_client)

    v2_broker = types.ModuleType("v2.broker")

    class AlpacaUnavailable(RuntimeError):
        pass

    v2_broker.AlpacaUnavailable = AlpacaUnavailable
    v2_broker.get_portfolio = lambda: {
        "positions": [{"symbol": s} for s in (alpaca_positions or [])],
    }
    monkeypatch.setitem(sys.modules, "v2.broker", v2_broker)

    v2_bot_pkg = types.ModuleType("v2.bot")
    v2_bot_pkg.__path__ = [str(_REPO_ROOT / "v2" / "bot")]
    v2_bot_state = types.ModuleType("v2.bot.state")
    v2_bot_state.watchlist_list = lambda: [
        {"ticker": t, "added_at": "2026-01-01T00:00:00+00:00", "note": ""}
        for t in (watchlist or [])
    ]
    v2_bot_pkg.state = v2_bot_state
    monkeypatch.setitem(sys.modules, "v2.bot", v2_bot_pkg)
    monkeypatch.setitem(sys.modules, "v2.bot.state", v2_bot_state)

    from v2.earnings import _bot_cards as cards

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
    v2_reporting.format_earnings_reminder = cards.format_earnings_reminder
    v2_reporting.format_earnings_summary = cards.format_earnings_summary
    v2_reporting.format_earnings_pending = cards.format_earnings_pending
    v2_reporting.format_earnings_view = cards.format_earnings_view
    v2_reporting.format_earnings_calendar = cards.format_earnings_calendar
    monkeypatch.setitem(sys.modules, "v2.reporting", v2_reporting)

    spec = importlib.util.spec_from_file_location(
        "v2.reporting.priority",
        _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    real_priority = importlib.util.module_from_spec(spec)
    sys.modules["v2.reporting.priority"] = real_priority
    spec.loader.exec_module(real_priority)
    v2_reporting.priority = real_priority


@pytest.fixture
def stub_calendar(monkeypatch):
    from v2.earnings import calendar as cal_mod

    payloads: dict[str, dict | BaseException] = {}

    def fake_ticker(t):
        return SimpleNamespace(calendar=payloads.get(t, {}))

    monkeypatch.setattr(cal_mod.yf, "Ticker", fake_ticker)
    return payloads


_TODAY_ISO = date.today().isoformat()


def _aapl_record(surprise="BEAT", eps_a=2.10, eps_e=1.95,
                 rev_a=9.5e10, rev_e=9.1e10, period="2026-06-30"):
    q = SimpleNamespace(
        eps_surprise=surprise,
        earnings_per_share=eps_a,
        estimated_earnings_per_share=eps_e,
        revenue=rev_a,
        estimated_revenue=rev_e,
    )
    return SimpleNamespace(
        ticker="AAPL", report_period=period,
        source_type="8-K", filing_date="2026-08-01",
        quarterly=q,
    )


def _patch_fd_class(monkeypatch, cron, fd_instance):
    class _Class:
        def __init__(self): pass
        def __enter__(self): return fd_instance
        def __exit__(self, *a): return False
    monkeypatch.setattr(cron, "CachedFDClient", _Class)


def _patch_summarizer(monkeypatch):
    """Disable LLM + transcript so the cron stays pure."""
    from v2.earnings import pipeline as p
    monkeypatch.setattr(
        p.summarizer_mod, "summarize",
        lambda *a, **k: ({"bull": "b", "bear": "x", "narrative": "n"}, 0),
    )
    monkeypatch.setattr(
        p.transcript_mod, "find_transcript",
        lambda *a, **k: None,
    )


def _patch_ten_q_fetcher(monkeypatch, return_value=None, raises=None):
    """Replace v2.earnings.pipeline._fetch_recent_ten_q so the cron
    doesn't reach SEC EDGAR. Pass return_value (TenQDelta or None) or
    raises=Exception() to simulate an outage."""
    from v2.earnings import pipeline as p

    def _stub(ticker, today_iso, **kw):
        if raises is not None:
            raise raises
        return return_value
    monkeypatch.setattr(p, "_fetch_recent_ten_q", _stub)


def _synthetic_ten_q(
    *, mda_paragraphs=None, new_rf=0,
    going_concern=False, material_weakness=False,
):
    from v2.sec.ten_q_parser import TenQDelta
    return TenQDelta(
        ticker="AAPL",
        filing_date="2026-05-20",
        period="Q2 2026",
        mda_added_paragraphs=list(mda_paragraphs or []),
        new_risk_factor_count=new_rf,
        has_going_concern=going_concern,
        has_material_weakness=material_weakness,
    )


class TestEarningsTenQIntegration:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def _setup_aapl(self, monkeypatch, temp_archive, stub_calendar):
        _install_earnings_cron_stubs(
            monkeypatch, alpaca_positions=["AAPL"], watchlist=[],
        )
        cron = _load_script("earnings_summaries.py")
        stub_calendar["AAPL"] = {"Earnings Date": [_TODAY_ISO]}
        record = _aapl_record()
        class _FD:
            def get_earnings(self, t): return record
            def get_earnings_history(self, t, limit=4): return [record]
        _patch_fd_class(monkeypatch, cron, _FD())
        _patch_summarizer(monkeypatch)
        return cron

    def test_earnings_summary_with_recent_10q_includes_mda_section(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """Ticker with recent 10-Q → ⑧ card contains '📋 10-Q MD&A 关键变化'."""
        cron = self._setup_aapl(monkeypatch, temp_archive, stub_calendar)
        _patch_ten_q_fetcher(monkeypatch, return_value=_synthetic_ten_q(
            mda_paragraphs=[
                "Revenue growth driven by Services and wearables…",
            ],
            new_rf=1,
        ))

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        text = call["text"]
        # 10-Q section header rendered (HTML-escaped: MD&amp;A)
        assert "<b>📋 10-Q MD&amp;A 关键变化</b>" in text
        # MD&A added paragraph rendered with ➕ prefix
        assert "Revenue growth driven by Services and wearables" in text
        # new risk factor count surfaced
        assert "1 个新 risk factor 段落" in text

    def test_earnings_summary_no_10q_silent_section(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """No recent 10-Q (_fetch_recent_ten_q returns None) → no
        10-Q section header, but earnings card still pushes."""
        cron = self._setup_aapl(monkeypatch, temp_archive, stub_calendar)
        _patch_ten_q_fetcher(monkeypatch, return_value=None)

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        text = rec.calls[0]["text"]
        assert "AAPL · BEAT" in rec.calls[0]["title"]
        # No 10-Q section header
        assert "10-Q MD&amp;A 关键变化" not in text
        assert "Going concern" not in text
        assert "Material weakness" not in text

    def test_going_concern_promotes_p0(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """has_going_concern=True → +20 priority bump pushes the card
        above the P0 score floor (80)."""
        cron = self._setup_aapl(monkeypatch, temp_archive, stub_calendar)
        _patch_ten_q_fetcher(monkeypatch, return_value=_synthetic_ten_q(
            going_concern=True,
        ))

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        # base earnings_summary=70 + held(+15) + going_concern(+20) = 105 → 100 (P0)
        assert call["priority_tier"] == "P0"
        assert call["priority_score"] == 100
        assert any("going_concern_in_10q" in r for r in call["priority_reasons"])
        # Card body shows the auditor flag line
        assert "Going concern" in call["text"]

    def test_material_weakness_priority_bump(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """has_material_weakness=True → +15 priority bump; combined with
        held +15 lands at base 70+15+15 = 100 → P0. Verifies the +15
        reason landed in the trail even after score saturation."""
        cron = self._setup_aapl(monkeypatch, temp_archive, stub_calendar)
        _patch_ten_q_fetcher(monkeypatch, return_value=_synthetic_ten_q(
            material_weakness=True,
        ))

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        # base 70 + 15 held + 15 material_weakness = 100 → P0
        assert call["priority_tier"] == "P0"
        assert call["priority_score"] == 100
        assert any(
            "material_weakness_in_10q" in r for r in call["priority_reasons"]
        )
        assert "Material weakness" in call["text"]

    def test_10q_lookup_failure_silent_skip(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """_fetch_recent_ten_q raising → earnings card still ships,
        10-Q section silently omitted. Priority stays at the base
        (no auditor-flag bump because flags never materialized)."""
        cron = self._setup_aapl(monkeypatch, temp_archive, stub_calendar)
        _patch_ten_q_fetcher(
            monkeypatch, raises=RuntimeError("EDGAR 503"),
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        # base 70 + held(+15) = 85 → P0 (the 7.7% surprise < 10% so no extra)
        # No going_concern / material_weakness reasons since fetch failed
        assert call["priority_tier"] == "P0"
        assert call["priority_score"] == 85
        text = call["text"]
        assert "10-Q MD&amp;A 关键变化" not in text
        assert not any(
            "going_concern" in r or "material_weakness" in r
            for r in call["priority_reasons"]
        )
