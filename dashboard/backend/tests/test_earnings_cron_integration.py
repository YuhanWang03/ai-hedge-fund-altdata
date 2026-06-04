"""Cron-script integration tests for the Earnings agent.

Loads ``scripts/earnings_reminders.py`` and ``scripts/earnings_summaries.py``
via :mod:`importlib.util` with ``sys.modules`` pre-stubs for the
production-only deps (``v2.data``, ``v2.broker``, ``v2.bot.state``,
the relevant ``v2.reporting`` re-exports), so the actual cron entry
points exercise their priority + archive wiring under test.

What's verified:

- The Mon-Fri 08:00 ET reminders cron passes the correct ``priority_tier``
  (P2 for D-3, P1 for D-1 / D-0) into the notifier, which in turn flows
  into ``archive.save_text``.
- The Mon-Fri 21:00 ET summaries cron promotes to P0 when
  ``|surprise_pct| ≥ 10%`` and marks the dedup row.
- The same cron emits a P2 pending placeholder when FD has no quarterly
  data yet, and the pending key (``pending_<today>``) doesn't collide
  with the eventual real ``report_period``.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Stub harness — install fake v2.data / v2.broker / v2.bot / v2.reporting
# ---------------------------------------------------------------------------

def _install_cron_stubs(monkeypatch, *, alpaca_positions=None, watchlist=None):
    """Pre-populate sys.modules so the cron scripts' top-level imports
    succeed. Production-only modules (v2.data, v2.broker) and packages
    whose ``__init__`` transitively pulls them (v2.bot, v2.reporting) are
    replaced with lightweight stubs that re-export only the symbols the
    crons actually use.

    Real modules still load for v2.archive / v2.earnings / v2.observability
    — those are sandbox-importable on their own.

    Returns the dict of stub objects for assertions.
    """
    # --- v2.data (must be a package — v2.backtesting.strategy does
    #     ``from v2.data.client import FDClient`` even though nothing on
    #     the cron's hot path needs it) ---
    v2_data_pkg = types.ModuleType("v2.data")
    v2_data_pkg.__path__ = []      # marks as package
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

    # --- v2.broker ---
    v2_broker = types.ModuleType("v2.broker")

    class AlpacaUnavailable(RuntimeError):
        pass

    v2_broker.AlpacaUnavailable = AlpacaUnavailable
    v2_broker.get_portfolio = lambda: {
        "positions": [{"symbol": s} for s in (alpaca_positions or [])],
    }
    monkeypatch.setitem(sys.modules, "v2.broker", v2_broker)

    # --- v2.bot + v2.bot.state ---
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

    # --- v2.reporting (real package has a matplotlib-heavy + v2.lateral-
    #     pulling __init__; we stub the names the crons import) ---
    from v2.earnings import _bot_cards as cards   # sandbox-safe

    v2_reporting = types.ModuleType("v2.reporting")
    v2_reporting.__path__ = [str(_REPO_ROOT / "v2" / "reporting")]

    # Real notify_on_error decorator — simple passthrough that calls .__call__
    # like the production wrapper does.
    def notify_on_error(name):
        def decorator(fn):
            from functools import wraps
            @wraps(fn)
            def wrapper(*a, **kw):
                return fn(*a, **kw)
            return wrapper
        return decorator

    class TelegramNotifier:
        """Stub — tests replace the class attribute on the loaded cron
        module so this default is never actually used."""
        def __init__(self, *a, **kw): pass
        def send_text(self, *a, **kw): pass

    v2_reporting.TelegramNotifier = TelegramNotifier
    v2_reporting.notify_on_error = notify_on_error
    v2_reporting.format_earnings_reminder = cards.format_earnings_reminder
    v2_reporting.format_earnings_summary = cards.format_earnings_summary
    v2_reporting.format_earnings_pending = cards.format_earnings_pending
    v2_reporting.format_earnings_view = cards.format_earnings_view
    v2_reporting.format_earnings_calendar = cards.format_earnings_calendar
    monkeypatch.setitem(sys.modules, "v2.reporting", v2_reporting)

    # --- v2.reporting.priority — load via importlib so we get the real
    # compute_importance (the BASE_SCORES are what the cron asserts on).
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "v2.reporting.priority",
        _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    real_priority = _ilu.module_from_spec(_spec)
    sys.modules["v2.reporting.priority"] = real_priority
    _spec.loader.exec_module(real_priority)
    v2_reporting.priority = real_priority

    return {
        "v2.data": v2_data_pkg,
        "v2.broker": v2_broker,
        "v2.bot.state": v2_bot_state,
        "v2.reporting": v2_reporting,
        "v2.reporting.priority": real_priority,
    }


def _load_script(script_name: str):
    """Load a scripts/*.py module after stubs are in place."""
    script_path = _REPO_ROOT / "scripts" / script_name
    mod_name = f"_cron_under_test_{script_name.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Stub TelegramNotifier (records what would have been pushed)
# ---------------------------------------------------------------------------

class _RecordingNotifier:
    """Drop-in for TelegramNotifier. Captures every send_text call.

    The cron passes ``priority=PriorityResult(...)`` which the real notifier
    forwards into ``archive.save_text(priority_tier=...)``. We snapshot the
    full keyword args so tests can assert on tier / score / title / tickers.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def send_text(self, text, *, trace=None, title=None, tickers=None,
                  priority=None, **extra):
        self.calls.append({
            "text": text,
            "title": title,
            "tickers": list(tickers or []),
            "priority_tier": priority.tier if priority else None,
            "priority_score": priority.score if priority else None,
            "priority_reasons": list(priority.reasons) if priority else [],
        })


# ---------------------------------------------------------------------------
# Fixture: temp archive that the cron's Archive() picks up
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_archive(monkeypatch, tmp_path):
    """Repoint v2.archive.store._DB_PATH / _IMG_ROOT into a temp dir."""
    from v2.archive import store as archive_store

    db = tmp_path / "archive.db"
    img = tmp_path / "img"
    monkeypatch.setattr(archive_store, "_DB_PATH", db)
    monkeypatch.setattr(archive_store, "_IMG_ROOT", img)
    return tmp_path


@pytest.fixture
def stub_calendar(monkeypatch):
    """Patch yfinance.Ticker on the calendar module."""
    from v2.earnings import calendar as cal_mod

    payloads: dict[str, dict | BaseException] = {}

    def fake_ticker(t):
        return SimpleNamespace(calendar=payloads.get(t, {}))

    monkeypatch.setattr(cal_mod.yf, "Ticker", fake_ticker)
    return payloads


def _future_iso(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


_TODAY_ISO = date.today().isoformat()


# ===========================================================================
# Reminders cron
# ===========================================================================

class TestEarningsRemindersCron:

    def _aapl_d3(self, stub_calendar):
        stub_calendar.update({
            "AAPL": {"Earnings Date": [_future_iso(3)],
                     "EPS Estimate": 1.51,
                     "Revenue Estimate": 94e9},
        })

    def _nvda_d1(self, stub_calendar):
        stub_calendar.update({
            "NVDA": {"Earnings Date": [_future_iso(1)]},
        })

    def _msft_d0(self, stub_calendar):
        stub_calendar.update({
            "MSFT": {"Earnings Date": [_future_iso(0)]},
        })

    def test_d3_watchlist_only_pushes_with_p2_tier(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """D-3, watchlist-only → priority_tier='P2' (base 45 + 10 = 55)."""
        _install_cron_stubs(monkeypatch, alpaca_positions=[], watchlist=["AAPL"])
        cron = _load_script("earnings_reminders.py")

        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        self._aapl_d3(stub_calendar)

        rc = cron.main()
        assert rc == 0
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["priority_tier"] == "P2"
        assert call["priority_score"] == 55           # 45 base + 10 watchlist
        assert "AAPL · D-3" in call["title"]
        assert call["tickers"] == ["AAPL"]
        assert any("watchlist" in r for r in call["priority_reasons"])

    def test_d3_held_position_promotes_to_p1(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """D-3 + held = 45 + 15 = 60 → P1 (matches the Stage-2 spec
        'held position bumps reminders by one tier when at the edge')."""
        _install_cron_stubs(monkeypatch, alpaca_positions=["AAPL"], watchlist=[])
        cron = _load_script("earnings_reminders.py")

        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        self._aapl_d3(stub_calendar)

        rc = cron.main()
        assert rc == 0
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["priority_tier"] == "P1"
        assert recorder.calls[0]["priority_score"] == 60

    def test_d1_uses_p1_tier(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        _install_cron_stubs(monkeypatch, alpaca_positions=[], watchlist=["NVDA"])
        cron = _load_script("earnings_reminders.py")
        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        self._nvda_d1(stub_calendar)

        assert cron.main() == 0
        assert len(recorder.calls) == 1
        # D-1 base 60 + 10 watchlist = 70 → P1
        assert recorder.calls[0]["priority_tier"] == "P1"
        assert recorder.calls[0]["priority_score"] == 70
        assert "NVDA · D-1" in recorder.calls[0]["title"]

    def test_d0_uses_p1_tier(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        _install_cron_stubs(monkeypatch, alpaca_positions=["MSFT"], watchlist=[])
        cron = _load_script("earnings_reminders.py")
        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        self._msft_d0(stub_calendar)

        assert cron.main() == 0
        assert len(recorder.calls) == 1
        # D-0 base 60 + 15 held = 75 → P1
        assert recorder.calls[0]["priority_tier"] == "P1"
        assert recorder.calls[0]["priority_score"] == 75
        assert "MSFT · D-0" in recorder.calls[0]["title"]

    def test_empty_universe_no_pushes(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        _install_cron_stubs(monkeypatch, alpaca_positions=[], watchlist=[])
        cron = _load_script("earnings_reminders.py")
        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        assert cron.main() == 0
        assert recorder.calls == []

    def test_per_ticker_failure_does_not_block_batch(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """One ticker raising inside notifier.send_text must not stop the
        other tickers from being pushed — Stage 2 caveat 1."""
        _install_cron_stubs(
            monkeypatch, alpaca_positions=[], watchlist=["AAPL", "NVDA"],
        )
        cron = _load_script("earnings_reminders.py")

        # Calendar gives both AAPL and NVDA reminders.
        stub_calendar.update({
            "AAPL": {"Earnings Date": [_future_iso(3)]},
            "NVDA": {"Earnings Date": [_future_iso(1)]},
        })

        class _PartialBoomNotifier(_RecordingNotifier):
            def send_text(self, text, **kw):
                if "AAPL" in (kw.get("title") or ""):
                    raise RuntimeError("telegram 503")
                super().send_text(text, **kw)

        recorder = _PartialBoomNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        # Should not raise; NVDA still pushed.
        assert cron.main() == 0
        titles = [c["title"] for c in recorder.calls]
        assert any("NVDA" in t for t in titles)
        # AAPL was attempted but raised → not in successful calls list.
        assert not any("AAPL" in t for t in titles)


# ===========================================================================
# Summaries cron
# ===========================================================================

class TestEarningsSummariesCron:

    def _aapl_record(self, surprise="BEAT", eps_a=2.10, eps_e=1.95,
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

    def _patch_fd_class(self, monkeypatch, cron, fd_instance):
        """Replace CachedFDClient on the cron module with a fixed instance."""
        class _Class:
            def __init__(self): pass
            def __enter__(self): return fd_instance
            def __exit__(self, *a): return False
        monkeypatch.setattr(cron, "CachedFDClient", _Class)

    def test_big_surprise_pushes_p0(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """|surprise| ≈ 7.7% < 10% → no big_surprise bump; held +15 still
        promotes to P0 via the universal bonus. Test that wiring."""
        _install_cron_stubs(monkeypatch, alpaca_positions=["AAPL"], watchlist=[])
        cron = _load_script("earnings_summaries.py")

        # Calendar must say AAPL releases today so the cron asks FD.
        stub_calendar["AAPL"] = {"Earnings Date": [_TODAY_ISO]}

        record = self._aapl_record()  # BEAT, eps surprise ≈ 7.7%
        class _FD:
            def get_earnings(self, t): return record
            def get_earnings_history(self, t, limit=4): return [record]

        self._patch_fd_class(monkeypatch, cron, _FD())

        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        # Disable LLM + transcript to keep the cron pure.
        from v2.earnings import pipeline as p
        monkeypatch.setattr(
            p.summarizer_mod, "summarize",
            lambda *a, **k: ({"bull": "b", "bear": "x", "narrative": "n"}, 0),
        )
        monkeypatch.setattr(
            p.transcript_mod, "find_transcript",
            lambda *a, **k: None,
        )

        assert cron.main() == 0
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        # base 70 + 15 held = 85 → P0 (the 7.7% surprise is < 10% so no extra)
        assert call["priority_tier"] == "P0"
        assert call["priority_score"] == 85
        assert "AAPL · BEAT" in call["title"]

    def test_big_surprise_with_held_caps_at_100(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """|surprise| = 12% → +30. Combined with held +15 caps at 100, P0."""
        _install_cron_stubs(monkeypatch, alpaca_positions=["AAPL"], watchlist=[])
        cron = _load_script("earnings_summaries.py")

        stub_calendar["AAPL"] = {"Earnings Date": [_TODAY_ISO]}
        # Use eps that puts surprise above 10%.
        record = self._aapl_record(eps_a=2.10, eps_e=1.80)  # ≈ 16.7%
        class _FD:
            def get_earnings(self, t): return record
            def get_earnings_history(self, t, limit=4): return [record]
        self._patch_fd_class(monkeypatch, cron, _FD())

        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        from v2.earnings import pipeline as p
        monkeypatch.setattr(
            p.summarizer_mod, "summarize",
            lambda *a, **k: ({"bull": "b", "bear": "x", "narrative": "n"}, 0),
        )
        monkeypatch.setattr(
            p.transcript_mod, "find_transcript",
            lambda *a, **k: None,
        )

        assert cron.main() == 0
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["priority_tier"] == "P0"
        # 70 + 30 big_surprise + 15 held = 115 → clamped to 100
        assert recorder.calls[0]["priority_score"] == 100
        assert any("big_surprise" in r for r in recorder.calls[0]["priority_reasons"])

    def test_pending_writes_p2(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """FD returns None for today's release → cron must push the
        '数据待落地' P2 card and mark the dedup row with key
        ``pending_<today>``."""
        _install_cron_stubs(monkeypatch, alpaca_positions=[], watchlist=["NVDA"])
        cron = _load_script("earnings_summaries.py")

        stub_calendar["NVDA"] = {"Earnings Date": [_TODAY_ISO]}

        class _FD:
            def get_earnings(self, t): return None
            def get_earnings_history(self, t, limit=4): return []
        self._patch_fd_class(monkeypatch, cron, _FD())

        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        assert cron.main() == 0
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        # earnings_pending base 45 + watchlist 10 = 55 → P2
        assert call["priority_tier"] == "P2"
        assert call["priority_score"] == 55
        assert "财报待落地 · NVDA" in call["title"]
        assert "数据待落地" in call["text"]

        # And the archive dedup table has a "pending_<today>" row that
        # is NOT in get_summarized_set (only outcome='summarized' counts).
        from v2.archive import Archive
        archive = Archive("earnings")
        # Pending row with the synthetic key
        assert archive.is_earnings_summarized("NVDA", f"pending_{_TODAY_ISO}") is False
        # Empty summarized set — tomorrow's real-data retry will still fire.
        assert archive.get_summarized_set() == set()

    def test_summarized_row_blocks_same_day_retry(
        self, monkeypatch, temp_archive, stub_calendar,
    ):
        """Run the cron twice with valid FD data. Second run must dedup
        and emit zero pushes (the get_summarized_set entry from the first
        run wins)."""
        _install_cron_stubs(monkeypatch, alpaca_positions=["AAPL"], watchlist=[])
        cron = _load_script("earnings_summaries.py")

        stub_calendar["AAPL"] = {"Earnings Date": [_TODAY_ISO]}
        record = self._aapl_record()
        class _FD:
            def get_earnings(self, t): return record
            def get_earnings_history(self, t, limit=4): return [record]
        self._patch_fd_class(monkeypatch, cron, _FD())

        recorder = _RecordingNotifier()
        monkeypatch.setattr(cron, "TelegramNotifier", lambda **kw: recorder)

        from v2.earnings import pipeline as p
        monkeypatch.setattr(
            p.summarizer_mod, "summarize",
            lambda *a, **k: ({"bull": "b", "bear": "x", "narrative": "n"}, 0),
        )
        monkeypatch.setattr(
            p.transcript_mod, "find_transcript",
            lambda *a, **k: None,
        )

        # First run — should push 1 card.
        assert cron.main() == 0
        assert len(recorder.calls) == 1

        # Second run — should push 0 (already-summarized dedup).
        assert cron.main() == 0
        assert len(recorder.calls) == 1, "second run should be deduped"

        # Archive carries the real (ticker, report_period) row.
        from v2.archive import Archive
        archive = Archive("earnings")
        assert archive.is_earnings_summarized("AAPL", "2026-06-30") is True
