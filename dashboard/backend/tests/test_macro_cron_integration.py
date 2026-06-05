"""Cron-script integration tests for the Macro agent (Phase 4 Stage 6).

Loads the 4 macro cron scripts via :mod:`importlib.util` with
``sys.modules`` pre-stubs for production-only deps (``v2.data``,
``v2.broker``, ``v2.reporting``) so the actual cron entry points run
end-to-end against a recording notifier and a temp-dir Archive.

Mirrors the Phase 3 SEC cron integration harness
(``test_sec_cron_integration.py``) — same _RecordingNotifier,
same _install_cron_stubs pattern, same temp_archive fixture.

What's verified end-to-end:

⑭ Macro Daily Snapshot (``macro_daily_snapshot.py``):
- Normal day → P3 ``macro_snapshot_p3`` push.
- VIX +25% spike → P0 ``macro_vix_spike`` with 🚨 +20% tag.
- Curve flip → P1 ``macro_curve_flip`` with 📉 今日翻转 tag.
- yfinance partial failure → snapshot.warnings populated, P3 still,
  warnings visible in card body.
- ``trace_json`` carries ``name='_r_macro_snapshot'`` framing event.
- Byte-equal: pushed text == ``format_macro_daily_snapshot(snap)``.

⑮ Macro Release (``macro_release_to_telegram.py``):
- No release today → silent skip, no notifier, no archive write.
- CPI σ=0.2 → P2 ``macro_release_p2``.
- CPI σ=3.5 → P0 ``macro_release_p0`` with ``extreme_surprise`` reason.
- FOMC + SEP hawkish_shift → P0 with ``sep_hawkish_shift`` reason.
- FOMC no SEP → P1 (base FOMC floor).
- PCE + GDP same day → 2 separate pushes (BEA co-release pattern).
- ``_r_macro_release`` responder name on trace.

⑯ Macro Claims (``macro_claims_to_telegram.py``):
- Normal Thursday → P2 ``macro_release_p2``.
- σ=2.5 surprise → P1 promotion via ``macro_release_p1`` kind.
- ``build_claims_event`` returns None (holiday week / FRED lag) →
  silent skip, no archive write.

⑰ Macro Weekly Recap (``macro_weekly_to_telegram.py``):
- Normal week → P1 floor via ``macro_weekly`` base 65.
- Quiet week (no releases) → still P1 (operator visibility design,
  matches ⑩ Portfolio Weekly posture).
- ``_r_macro_weekly`` responder name on trace.

Architecture guard:
- Source-of-truth invariant: ``grep _format_macro`` across all 4 cron
  scripts + ``v2/bot/responders.py`` returns 0 hits. The Stage 5 lift
  is irreversible.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Module-level sandbox stubs (defensive — v2/conftest.py covers edgar /
# langchain_deepseek / tavily globally; we add fredapi here since the
# macro path's fred_client lazy-imports it).
# ---------------------------------------------------------------------------

for _mod_name in ("edgar", "langchain_deepseek", "tavily", "fredapi"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()


# ---------------------------------------------------------------------------
# Recording notifier — kept in sync with TelegramNotifier._archive_with_priority
# ---------------------------------------------------------------------------

class _RecordingNotifier:
    """Drop-in for ``TelegramNotifier``. Captures send_text + mirrors
    the archive-write side-effect. Same shape as the Phase 2 / Phase 3
    integration test stubs — if the real notifier's
    ``_archive_with_priority`` changes, this stub must change with it.
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
    snapshot=None,
    snapshot_raises=None,
    release_report=None,
    release_today=None,
    claims_release=None,
    weekly_recap=None,
):
    """Stub v2.data / v2.broker / v2.reporting / v2.macro entry points
    for one cron run. Each cron picks up only the stubs relevant to its
    path; the unused ones are harmless.

    Args:
        snapshot: MacroSnapshot returned by build_macro_snapshot (⑭).
        snapshot_raises: optional exception class for build_macro_snapshot.
        release_report: MacroReport returned by build_release_event (⑮).
        release_today: list returned by get_release_today (⑮ gate).
        claims_release: MacroRelease returned by build_claims_event (⑯).
        weekly_recap: dict returned by build_weekly_recap (⑰).
    """
    # --- v2.data shell ---
    v2_data = types.ModuleType("v2.data")
    v2_data.__path__ = []
    monkeypatch.setitem(sys.modules, "v2.data", v2_data)

    # --- v2.broker ---
    v2_broker = types.ModuleType("v2.broker")

    class AlpacaUnavailable(RuntimeError):
        pass

    v2_broker.AlpacaUnavailable = AlpacaUnavailable
    v2_broker.get_portfolio = lambda: {"positions": []}
    monkeypatch.setitem(sys.modules, "v2.broker", v2_broker)

    # --- v2.bot.state (cron scripts don't use it but the bot module's
    # import chain is defensive-stubbed for harness uniformity) ---
    if "v2.bot" not in sys.modules or not hasattr(sys.modules.get("v2.bot"), "state"):
        v2_bot = types.ModuleType("v2.bot")
        v2_bot.__path__ = []
        v2_bot_state = types.ModuleType("v2.bot.state")
        v2_bot_state.watchlist_list = lambda: []
        v2_bot.state = v2_bot_state
        monkeypatch.setitem(sys.modules, "v2.bot", v2_bot)
        monkeypatch.setitem(sys.modules, "v2.bot.state", v2_bot_state)

    # --- v2.reporting stub wired through v2.macro._bot_cards source-of-truth ---
    from v2.macro import _bot_cards as macro_cards

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
    v2_reporting.format_macro_daily_snapshot = macro_cards.format_macro_daily_snapshot
    v2_reporting.format_macro_release_card = macro_cards.format_macro_release_card
    v2_reporting.format_macro_fomc_card = macro_cards.format_macro_fomc_card
    v2_reporting.format_macro_claims_card = macro_cards.format_macro_claims_card
    v2_reporting.format_macro_weekly_recap = macro_cards.format_macro_weekly_recap
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

    # --- Patch v2.macro entry points (real package exists; we replace
    # the specific functions the crons import). ---
    import v2.macro as _macro_pkg
    import v2.macro.pipeline as _macro_pipe
    import v2.macro.release_calendar as _cal_mod

    if snapshot_raises is not None:
        def _snap_raise(_iso):
            raise snapshot_raises
        monkeypatch.setattr(_macro_pkg, "build_macro_snapshot", _snap_raise)
        monkeypatch.setattr(_macro_pipe, "build_macro_snapshot", _snap_raise)
    elif snapshot is not None:
        monkeypatch.setattr(_macro_pkg, "build_macro_snapshot",
                            lambda _iso: snapshot)
        monkeypatch.setattr(_macro_pipe, "build_macro_snapshot",
                            lambda _iso: snapshot)

    if release_report is not None:
        monkeypatch.setattr(_macro_pkg, "build_release_event",
                            lambda _iso: release_report)
        monkeypatch.setattr(_macro_pipe, "build_release_event",
                            lambda _iso: release_report)

    if release_today is not None:
        monkeypatch.setattr(_cal_mod, "get_release_today",
                            lambda _iso: list(release_today))

    if claims_release is not None or claims_release is False:
        # ``False`` sentinel means "build_claims_event returns None"
        if claims_release is False:
            target = None
        else:
            target = claims_release
        monkeypatch.setattr(_macro_pkg, "build_claims_event",
                            lambda _iso: target)
        monkeypatch.setattr(_macro_pipe, "build_claims_event",
                            lambda _iso: target)

    if weekly_recap is not None:
        monkeypatch.setattr(_macro_pkg, "build_weekly_recap",
                            lambda _iso: weekly_recap)
        monkeypatch.setattr(_macro_pipe, "build_weekly_recap",
                            lambda _iso: weekly_recap)


def _load_script(script_name: str):
    """Load a scripts/macro_*.py module after stubs are in place."""
    script_path = _REPO_ROOT / "scripts" / script_name
    mod_name = f"_p4_cron_under_test_{script_name.replace('.', '_')}"
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
# Synthetic fixtures — share shape with Stage 5 byte-equal pins
# ---------------------------------------------------------------------------

def _normal_snapshot():
    from v2.macro.models import MacroSnapshot
    return MacroSnapshot(
        snapshot_date="2026-06-12",
        vix=14.20, vix_pct_change_1d=0.008,
        dxy=99.50, wti_crude=78.40, gold=2650.5,
        fed_funds_upper=5.50, fed_funds_lower=5.25,
        dgs2=4.21, dgs10=4.42, t10y2y=0.21, t10y2y_prior=0.18,
    )


def _vix_spike_snapshot():
    from v2.macro.models import MacroSnapshot
    return MacroSnapshot(
        snapshot_date="2026-06-13",
        vix=28.50, vix_pct_change_1d=0.25,
        vix_spike=True, vix_elevated=True,
        dxy=99.5, wti_crude=78.4, gold=2650.5,
        fed_funds_upper=5.50, fed_funds_lower=5.25,
        dgs2=4.21, dgs10=4.42, t10y2y=-0.10, t10y2y_prior=0.05,
        curve_flip=True,
    )


def _curve_flip_only_snapshot():
    """Curve flipped without VIX spike — exercises the curve_flip
    routing path independently of vix_spike."""
    from v2.macro.models import MacroSnapshot
    return MacroSnapshot(
        snapshot_date="2026-06-14",
        vix=18.0, vix_pct_change_1d=0.03,
        dxy=99.5, wti_crude=78.4, gold=2650.5,
        fed_funds_upper=5.50, fed_funds_lower=5.25,
        dgs2=4.30, dgs10=4.25, t10y2y=-0.05, t10y2y_prior=0.02,
        curve_flip=True,
    )


def _partial_failure_snapshot():
    """yfinance partial failure: VIX + WTI missing, FRED rates OK."""
    from v2.macro.models import MacroSnapshot
    return MacroSnapshot(
        snapshot_date="2026-06-12",
        vix=None, vix_pct_change_1d=None,
        dxy=99.5, wti_crude=None, gold=2650.5,
        fed_funds_upper=5.50, fed_funds_lower=5.25,
        dgs2=4.21, dgs10=4.42, t10y2y=0.21, t10y2y_prior=0.18,
        warnings=["yfinance VIX: HTTPError", "yfinance WTI: TimeoutException"],
    )


def _cpi_in_line_release():
    from v2.macro.models import MacroRelease
    return MacroRelease(
        release_type="CPI", release_date="2026-06-10",
        period="CPI May 2026",
        headline=320.5, core=315.2,
        mom_pct=0.003, yoy_pct=0.029,
        consensus=0.003, surprise_sigma=0.2, surprise_label="in_line",
        trailing_3mo_trend="decelerating",
        bull_takeaway="核心 YoY 连续 3 月放缓",
        bear_takeaway="MoM 仍高于 Fed 目标",
        narrative="Headline 与预期持平",
        tone="neutral",
    )


def _cpi_extreme_release():
    from v2.macro.models import MacroRelease
    return MacroRelease(
        release_type="CPI", release_date="2026-06-10",
        period="CPI May 2026",
        headline=320.5, core=315.2,
        mom_pct=0.005, yoy_pct=0.035,
        consensus=0.003, surprise_sigma=3.5,
        surprise_label="extreme_above_3sigma",
        trailing_3mo_trend="accelerating",
        bear_takeaway="3σ 以上向上偏离",
        narrative="通胀大幅高于预期",
        tone="hawkish",
    )


def _pce_release():
    from v2.macro.models import MacroRelease
    return MacroRelease(
        release_type="PCE", release_date="2026-06-25",
        period="PCE May 2026",
        headline=0.002, mom_pct=0.002, yoy_pct=0.026,
        consensus=0.002, surprise_sigma=0.0, surprise_label="in_line",
        trailing_3mo_trend="flat",
        narrative="PCE 持平预期", tone="neutral",
    )


def _gdp_release():
    from v2.macro.models import MacroRelease
    return MacroRelease(
        release_type="GDP", release_date="2026-06-25",
        period="GDP Q1 2026",
        headline=0.028, mom_pct=0.007, yoy_pct=0.028,
        consensus=0.025, surprise_sigma=0.6, surprise_label="in_line",
        trailing_3mo_trend="accelerating",
        narrative="GDP 略超预期", tone="neutral",
    )


def _fomc_may_no_sep():
    from v2.macro.models import FOMCEvent
    return FOMCEvent(
        meeting_date="2026-05-07",
        statement_diff={
            "added_phrases": ["elevated"],
            "removed_phrases": ["modest"],
            "unchanged_phrases": [],
        },
        has_sep=False,
        sep_median_dots=None,
        sep_dot_plot_change="no_change",
        sell_side_sentiment="hawkish",
        sell_side_sources=["reuters.com", "bloomberg.com"],
    )


def _fomc_jun_hawkish_sep():
    from v2.macro.models import FOMCEvent
    return FOMCEvent(
        meeting_date="2026-06-17",
        statement_diff={
            "added_phrases": ["additional policy firming"],
            "removed_phrases": ["data-dependent"],
            "unchanged_phrases": [],
        },
        has_sep=True,
        sep_median_dots={2026: 4.00, 2027: 3.50, "longer_run": 2.875},
        sep_dot_plot_change="hawkish_shift",
        sell_side_sentiment="hawkish",
        sell_side_sources=["reuters.com", "bloomberg.com", "wsj.com"],
    )


def _claims_normal_release():
    from v2.macro.models import MacroRelease
    return MacroRelease(
        release_type="Claims", release_date="2026-06-18",
        period="Initial Claims",
        headline=242000, core=236500, prior_value=228000,
        surprise_sigma=0.3, surprise_label="in_line",
        trailing_3mo_trend="flat",
        narrative="周度初请基本持平",
        tone="neutral",
    )


def _claims_surprise_release():
    from v2.macro.models import MacroRelease
    return MacroRelease(
        release_type="Claims", release_date="2026-06-18",
        period="Initial Claims",
        headline=275000, core=255000, prior_value=228000,
        consensus=235000, surprise_sigma=2.5,
        surprise_label="above_2sigma",
        trailing_3mo_trend="accelerating",
        narrative="周度初请跳升 47K",
        tone="hawkish",
        bear_takeaway="劳动力市场冷却信号",
    )


def _weekly_recap_normal():
    return {
        "week_start": "2026-06-08",
        "week_end": "2026-06-12",
        "weekly_deltas": {
            "VIXCLS": 2.10, "DGS10": -0.05,
            "DGS2": -0.02, "T10Y2Y": -0.03,
        },
        "this_week_releases": {
            "2026-06-10": [("CPI", "CPI May 2026", "BLS")],
            "2026-06-11": [("PPI", "PPI May 2026", "BLS")],
        },
        "next_week_releases": {
            "2026-06-17": [("FOMC", "Jun FOMC + SEP", "Fed")],
        },
    }


def _weekly_recap_quiet():
    return {
        "week_start": "2026-07-06",
        "week_end": "2026-07-10",
        "weekly_deltas": {
            "VIXCLS": 0.20, "DGS10": 0.01,
            "DGS2": 0.00, "T10Y2Y": 0.01,
        },
        "this_week_releases": {},
        "next_week_releases": {},
    }


# ===========================================================================
# ⑭ Macro Daily Snapshot
# ===========================================================================

class TestMacroSnapshotCron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def test_cron14_normal_day_pushes_p3(self, monkeypatch, temp_archive):
        _install_cron_stubs(monkeypatch, snapshot=_normal_snapshot())
        cron = _load_script("macro_daily_snapshot.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["priority_tier"] == "P3"
        assert call["priority_score"] == 35
        assert "宏观日终" in call["title"]
        # Normal snapshot icon
        assert "📊 宏观日终" in call["text"]
        # 10Y-2Y rendered in bps per Stage 5 UX polish
        assert "+21bp" in call["text"]

    def test_cron14_vix_spike_promotes_p0(self, monkeypatch, temp_archive):
        _install_cron_stubs(monkeypatch, snapshot=_vix_spike_snapshot())
        cron = _load_script("macro_daily_snapshot.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        call = rec.calls[0]
        assert call["priority_tier"] == "P0"
        # base macro_vix_spike=85 + vix_strong(+10) → 95
        assert call["priority_score"] == 95
        # 🚨 +20% tag visible
        assert "🚨 +20%" in call["text"]
        # Curve flip ALSO present (kind routing picks vix_spike since
        # it's higher priority, but the card body shows both flags)
        assert "📉 今日翻转" in call["text"]
        assert any("vix_strong" in r for r in call["priority_reasons"])

    def test_cron14_curve_flip_promotes_p1(self, monkeypatch, temp_archive):
        """Curve flip without VIX spike → routed to macro_curve_flip."""
        _install_cron_stubs(monkeypatch, snapshot=_curve_flip_only_snapshot())
        cron = _load_script("macro_daily_snapshot.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        call = rec.calls[0]
        assert call["priority_tier"] == "P1"
        # base macro_curve_flip=65 + yield_curve_inverted(+10) → 75
        assert call["priority_score"] == 75
        assert "📉 今日翻转" in call["text"]
        assert any("yield_curve_inverted" in r for r in call["priority_reasons"])

    def test_cron14_partial_failure_warnings_visible(
        self, monkeypatch, temp_archive,
    ):
        """yfinance partial failure → snapshot.warnings populated, P3
        push still goes through, warnings rendered in card body."""
        _install_cron_stubs(monkeypatch, snapshot=_partial_failure_snapshot())
        cron = _load_script("macro_daily_snapshot.py")
        rc, rec = self._run(monkeypatch, cron)

        assert rc == 0
        assert len(rec.calls) == 1, "must still push exactly one card"
        call = rec.calls[0]
        # No anomaly flag → default P3
        assert call["priority_tier"] == "P3"
        # Warnings rendered
        assert "数据不全" in call["text"]
        assert "yfinance VIX" in call["text"]

    def test_cron14_archive_trace_json_written(
        self, monkeypatch, temp_archive,
    ):
        _install_cron_stubs(monkeypatch, snapshot=_normal_snapshot())
        cron = _load_script("macro_daily_snapshot.py")
        rc, _rec = self._run(monkeypatch, cron)
        assert rc == 0

        import sqlite3, json
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        row = conn.execute(
            "SELECT trace_json FROM pushes WHERE agent='macro' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        events = json.loads(row[0])
        names = {e.get("name") for e in events
                 if e.get("type") in ("module_enter", "module_exit")}
        assert "_r_macro_snapshot" in names

    def test_cron14_byte_equal_card_matches_formatter(
        self, monkeypatch, temp_archive,
    ):
        """⑭ pushed text == format_macro_daily_snapshot(snap) byte-equal."""
        from v2.macro._bot_cards import format_macro_daily_snapshot
        snap = _normal_snapshot()
        _install_cron_stubs(monkeypatch, snapshot=snap)
        cron = _load_script("macro_daily_snapshot.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert rec.calls[0]["text"] == format_macro_daily_snapshot(snap), (
            "cron output diverges from public formatter"
        )


# ===========================================================================
# ⑮ Macro Release
# ===========================================================================

class TestMacroReleaseCron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def test_cron15_no_release_today_silent_skip(
        self, monkeypatch, temp_archive,
    ):
        """get_release_today returns [] → no notifier instantiated."""
        _install_cron_stubs(monkeypatch, release_today=[])
        cron = _load_script("macro_release_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert rec is None, "notifier should not be created on quiet days"
        # No archive row
        import sqlite3
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM pushes WHERE agent='macro'"
            ).fetchone()
        except sqlite3.OperationalError:
            rows = (0,)
        finally:
            conn.close()
        assert rows[0] == 0

    def test_cron15_cpi_in_line_p2(self, monkeypatch, temp_archive):
        from v2.macro.models import MacroReport
        report = MacroReport(report_date="2026-06-10")
        report.today_releases = [_cpi_in_line_release()]
        _install_cron_stubs(
            monkeypatch,
            release_today=[("CPI", "CPI May 2026", "BLS")],
            release_report=report,
        )
        cron = _load_script("macro_release_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["priority_tier"] == "P2"
        assert call["priority_score"] == 55
        assert "CPI" in call["text"]
        assert "+0.30%" in call["text"]   # consensus per Stage 5 polish

    def test_cron15_cpi_3sigma_surprise_p0(self, monkeypatch, temp_archive):
        from v2.macro.models import MacroReport
        report = MacroReport(report_date="2026-06-10")
        report.today_releases = [_cpi_extreme_release()]
        _install_cron_stubs(
            monkeypatch,
            release_today=[("CPI", "CPI May 2026", "BLS")],
            release_report=report,
        )
        cron = _load_script("macro_release_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        assert call["priority_tier"] == "P0"
        # base macro_release_p0=85 + extreme_surprise(+20) → 100 clamped
        assert call["priority_score"] == 100
        assert any("extreme_surprise" in r for r in call["priority_reasons"])
        assert "+3.5σ" in call["text"]
        assert "extreme_above_3sigma" in call["text"]

    def test_cron15_fomc_with_sep_hawkish_p0(
        self, monkeypatch, temp_archive,
    ):
        from v2.macro.models import MacroReport
        report = MacroReport(report_date="2026-06-17")
        report.fomc_event = _fomc_jun_hawkish_sep()
        _install_cron_stubs(
            monkeypatch,
            release_today=[("FOMC", "Jun FOMC + SEP", "Fed")],
            release_report=report,
        )
        cron = _load_script("macro_release_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        assert call["priority_tier"] == "P0"
        # base macro_release_p0=85 + sep_hawkish_shift(+15) +
        # sell_side_hawkish(+10) → 110 → clamped 100
        assert call["priority_score"] == 100
        assert any("sep_hawkish_shift" in r for r in call["priority_reasons"])
        # FOMC-specific card content
        assert "🏛️ FOMC" in call["text"]
        assert "additional policy firming" in call["text"]
        assert "SEP Dot Plot" in call["text"]
        assert "卖方读数" in call["text"]

    def test_cron15_fomc_no_sep_p1(self, monkeypatch, temp_archive):
        """May FOMC without SEP → P1 (base macro_release_p1=65 + sell-side
        hawkish nudge → 75 → P1)."""
        from v2.macro.models import MacroReport
        report = MacroReport(report_date="2026-05-07")
        report.fomc_event = _fomc_may_no_sep()
        _install_cron_stubs(
            monkeypatch,
            release_today=[("FOMC", "May FOMC", "Fed")],
            release_report=report,
        )
        cron = _load_script("macro_release_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        assert call["priority_tier"] == "P1"
        assert call["priority_score"] == 75    # 65 + 10 sell_side_hawkish
        # Card body says no SEP
        assert "无变动" not in call["text"]    # diff IS present
        assert "elevated" in call["text"]

    def test_cron15_multiple_releases_same_day(
        self, monkeypatch, temp_archive,
    ):
        """PCE + GDP co-released on 2026-06-25 → 2 separate pushes."""
        from v2.macro.models import MacroReport
        report = MacroReport(report_date="2026-06-25")
        report.today_releases = [_pce_release(), _gdp_release()]
        _install_cron_stubs(
            monkeypatch,
            release_today=[
                ("PCE", "PCE May 2026", "BEA"),
                ("GDP", "GDP Q1 2026", "BEA"),
            ],
            release_report=report,
        )
        cron = _load_script("macro_release_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 2, "PCE + GDP should produce 2 cards"
        titles = {c["title"] for c in rec.calls}
        assert any("PCE" in t for t in titles)
        assert any("GDP" in t for t in titles)

    def test_cron15_responder_name_correct(self, monkeypatch, temp_archive):
        from v2.macro.models import MacroReport
        report = MacroReport(report_date="2026-06-10")
        report.today_releases = [_cpi_in_line_release()]
        _install_cron_stubs(
            monkeypatch,
            release_today=[("CPI", "CPI May 2026", "BLS")],
            release_report=report,
        )
        cron = _load_script("macro_release_to_telegram.py")
        rc, _rec = self._run(monkeypatch, cron)
        assert rc == 0

        import sqlite3, json
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        row = conn.execute(
            "SELECT trace_json FROM pushes WHERE agent='macro' LIMIT 1"
        ).fetchone()
        conn.close()
        events = json.loads(row[0])
        names = {e.get("name") for e in events
                 if e.get("type") in ("module_enter", "module_exit")}
        assert "_r_macro_release" in names

    def test_cron15_byte_equal_release_card(self, monkeypatch, temp_archive):
        """Pushed CPI card == format_macro_release_card(release, tier='P2')."""
        from v2.macro._bot_cards import format_macro_release_card
        from v2.macro.models import MacroReport
        release = _cpi_in_line_release()
        report = MacroReport(report_date="2026-06-10")
        report.today_releases = [release]
        _install_cron_stubs(
            monkeypatch,
            release_today=[("CPI", "CPI May 2026", "BLS")],
            release_report=report,
        )
        cron = _load_script("macro_release_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        expected = format_macro_release_card(release, tier="P2")
        assert rec.calls[0]["text"] == expected


# ===========================================================================
# ⑯ Macro Initial Claims
# ===========================================================================

class TestMacroClaimsCron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def test_cron16_normal_p2(self, monkeypatch, temp_archive):
        _install_cron_stubs(monkeypatch, claims_release=_claims_normal_release())
        cron = _load_script("macro_claims_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["priority_tier"] == "P2"
        assert call["priority_score"] == 55
        assert "Initial Claims" in call["text"]
        # 4W MA smoothed level visible
        assert "236,500" in call["text"]

    def test_cron16_2sigma_surprise_p1(self, monkeypatch, temp_archive):
        """σ ≥ 2 → routed to macro_release_p1 kind → P1."""
        _install_cron_stubs(
            monkeypatch, claims_release=_claims_surprise_release(),
        )
        cron = _load_script("macro_claims_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        assert call["priority_tier"] == "P1"
        # base macro_release_p1=65 + big_surprise(+10 @ 2.5σ) → 75
        assert call["priority_score"] == 75
        assert any("big_surprise" in r for r in call["priority_reasons"])

    def test_cron16_no_data_silent_skip(self, monkeypatch, temp_archive):
        """Holiday week / FRED lag → build_claims_event returns None →
        cron logs and exits silently."""
        _install_cron_stubs(monkeypatch, claims_release=False)
        cron = _load_script("macro_claims_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert rec is None, "no notifier when no data"
        # No archive row
        import sqlite3
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM pushes WHERE agent='macro'"
            ).fetchone()
        except sqlite3.OperationalError:
            rows = (0,)
        finally:
            conn.close()
        assert rows[0] == 0


# ===========================================================================
# ⑰ Macro Weekly Recap
# ===========================================================================

class TestMacroWeeklyCron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    def test_cron17_normal_week_p1_floor(self, monkeypatch, temp_archive):
        _install_cron_stubs(monkeypatch, weekly_recap=_weekly_recap_normal())
        cron = _load_script("macro_weekly_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        assert len(rec.calls) == 1
        call = rec.calls[0]
        # macro_weekly base 65 → P1 floor
        assert call["priority_tier"] == "P1"
        assert call["priority_score"] == 65
        # Stage 5 UX polish: VIX in pts, DGS in bps
        assert "+2.10 pts" in call["text"]
        assert "-5bp" in call["text"]

    def test_cron17_quiet_week_still_p1(self, monkeypatch, temp_archive):
        """No releases → still P1 (operator visibility design)."""
        _install_cron_stubs(monkeypatch, weekly_recap=_weekly_recap_quiet())
        cron = _load_script("macro_weekly_to_telegram.py")
        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        call = rec.calls[0]
        assert call["priority_tier"] == "P1"
        assert call["priority_score"] == 65
        assert "本周无 release 触发" in call["text"]
        assert "下周无重大 release" in call["text"]

    def test_cron17_responder_name_correct(self, monkeypatch, temp_archive):
        _install_cron_stubs(monkeypatch, weekly_recap=_weekly_recap_normal())
        cron = _load_script("macro_weekly_to_telegram.py")
        rc, _rec = self._run(monkeypatch, cron)
        assert rc == 0

        import sqlite3, json
        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        row = conn.execute(
            "SELECT trace_json FROM pushes WHERE agent='macro' LIMIT 1"
        ).fetchone()
        conn.close()
        events = json.loads(row[0])
        names = {e.get("name") for e in events
                 if e.get("type") in ("module_enter", "module_exit")}
        assert "_r_macro_weekly" in names


# ===========================================================================
# Architecture guard — Stage 5 lift integrity
# ===========================================================================

class TestArchitectureGuard:

    def test_no_inline_format_macro_in_cron_scripts(self):
        """grep verify: scripts/macro_*.py + v2/bot/responders.py
        contain 0 inline ``_format_macro_*`` / ``_format_snapshot_*`` /
        ``_format_release_*`` / ``_format_fomc_*`` / ``_format_claims_*``
        / ``_format_weekly_*`` definitions. Stage 5 lift is irreversible.
        """
        forbidden_patterns = (
            "def _format_macro",
            "def _format_snapshot_card",
            "def _format_release_card",
            "def _format_fomc_card",
            "def _format_claims_card",
            "def _format_weekly_card",
            "def _format_macro_view_card",
            "def _format_release_check_card",
            "def _format_fomc_check_card",
        )
        files = [
            _REPO_ROOT / "scripts" / "macro_daily_snapshot.py",
            _REPO_ROOT / "scripts" / "macro_release_to_telegram.py",
            _REPO_ROOT / "scripts" / "macro_claims_to_telegram.py",
            _REPO_ROOT / "scripts" / "macro_weekly_to_telegram.py",
            _REPO_ROOT / "v2" / "bot" / "responders.py",
        ]
        offenders: list[str] = []
        for path in files:
            src = path.read_text(encoding="utf-8")
            for pat in forbidden_patterns:
                if pat in src:
                    offenders.append(f"{path.name}: {pat!r}")
        assert not offenders, (
            "Stage 5 lift regression — inline formatter helper(s) reintroduced:\n  "
            + "\n  ".join(offenders)
        )

    def test_cron_imports_go_through_v2_reporting(self):
        """All 4 cron scripts must import their formatter from
        v2.reporting (the public API), NOT directly from
        v2.macro._bot_cards (the source-of-truth's private module)."""
        cron_files = (
            "macro_daily_snapshot.py",
            "macro_release_to_telegram.py",
            "macro_claims_to_telegram.py",
            "macro_weekly_to_telegram.py",
        )
        for fname in cron_files:
            src = (_REPO_ROOT / "scripts" / fname).read_text()
            assert "from v2.reporting import" in src, (
                f"{fname}: missing v2.reporting import"
            )
            assert "format_macro_" in src, (
                f"{fname}: doesn't reference any format_macro_* function"
            )
            assert "from v2.macro._bot_cards" not in src, (
                f"{fname}: imports source-of-truth directly; use v2.reporting"
            )
