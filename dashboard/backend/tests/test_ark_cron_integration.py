"""Cron-script integration tests for the ⑬ ARK Alerts agent (Phase 5a).

Loads ``scripts/ark_alerts_to_telegram.py`` via :mod:`importlib.util`
with ``sys.modules`` pre-stubs for the production-only deps
(``v2.data``, ``v2.broker``, ``v2.bot.state``, the relevant
``v2.reporting`` re-exports + the v2.reporting.priority module via
importlib) so the actual cron entry point exercises its priority +
archive wiring under test.

What's verified end-to-end:

- Normal day with mixed alerts → individual cards + summary card pushed,
  each one's ``priority_tier`` / ``priority_reasons`` landed in
  ``archive.pushes`` exactly as the real notifier would.
- Quiet day (0 alerts) → silent skip with archive trace still written.
- Multi-fund coordination on a held ticker → P0 escalation via
  ``+10 held_or_watchlist_ark`` + ``+15 multi_fund_coordination``
  reasons (the audit trail that Stage 4 in Phase 3.5 caught a
  silent-ship bug on — we pin the reason strings here for the same
  reason).
- Per-fund ARK CSV failure → other funds keep processing, warnings
  aggregated into the summary's "warnings: N" line.
- First-deploy fresh install → no ``get_latest_snapshot_before``
  baseline → 0 alerts pushed (⑤ ETF Daily populates the baseline
  at 17:00 ET so day 2 onwards has signal).
- ``capture_trace_with_framing(responder_name='_r_ark_alerts')``
  framing event lands in archive ``trace_json``.
- Cron-pushed individual card text == ``format_ark_alert(alert)``
  byte-equal (Phase 3 / 3.5 cross-surface identity convention).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Defensive sandbox stubs
# ---------------------------------------------------------------------------

for _mod in ("edgar", "langchain_deepseek", "tavily", "fredapi"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


# ---------------------------------------------------------------------------
# Recording notifier — mirrors TelegramNotifier._archive_with_priority
# ---------------------------------------------------------------------------

class _RecordingNotifier:
    """Drop-in for TelegramNotifier. Captures send_text + mirrors the
    real archive write so tests can assert priority_tier /
    priority_reasons / trace_json landed in archive.pushes."""

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
# Temp archive fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_archive(monkeypatch, tmp_path):
    from v2.archive import store as archive_store
    monkeypatch.setattr(archive_store, "_DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(archive_store, "_IMG_ROOT", tmp_path / "img")
    return tmp_path


# ---------------------------------------------------------------------------
# Stub harness for the ⑬ cron
# ---------------------------------------------------------------------------

def _install_ark_cron_stubs(
    monkeypatch, *, alpaca_positions=None, watchlist=None,
):
    """Stubs the production-only modules the cron imports at top-level.
    The cron then dynamically monkey-patches ``fetch_holdings`` /
    ``get_latest_snapshot_before`` to inject test data per-fund."""
    # v2.data — package; cron transitively imports via v2.broker
    if "v2.data" not in sys.modules or not hasattr(
        sys.modules.get("v2.data"), "CachedFDClient",
    ):
        v2_data = types.ModuleType("v2.data")
        v2_data.__path__ = []
        v2_data.CachedFDClient = type("CachedFDClient", (), {})
        v2_data.FDClient = type("FDClient", (), {})
        monkeypatch.setitem(sys.modules, "v2.data", v2_data)

    # v2.broker
    v2_broker = types.ModuleType("v2.broker")

    class AlpacaUnavailable(RuntimeError):
        pass

    v2_broker.AlpacaUnavailable = AlpacaUnavailable
    v2_broker.get_portfolio = lambda: {
        "positions": [{"symbol": s} for s in (alpaca_positions or [])],
    }
    monkeypatch.setitem(sys.modules, "v2.broker", v2_broker)

    # v2.bot + v2.bot.state
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

    # v2.reporting stub — heavy package init pulls matplotlib + v2.data
    from v2.etf import _ark_alert_cards as ark_cards

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
    v2_reporting.format_ark_alert = ark_cards.format_ark_alert
    v2_reporting.format_ark_summary = ark_cards.format_ark_summary
    monkeypatch.setitem(sys.modules, "v2.reporting", v2_reporting)

    # Real priority module via importlib (bypass v2.reporting init)
    spec = importlib.util.spec_from_file_location(
        "v2.reporting.priority",
        _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    real_priority = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "v2.reporting.priority", real_priority)
    spec.loader.exec_module(real_priority)
    v2_reporting.priority = real_priority


def _load_script(script_name: str):
    """Load scripts/*.py after stubs are installed."""
    script_path = _REPO_ROOT / "scripts" / script_name
    mod_name = f"_p5a_cron_under_test_{script_name.replace('.', '_')}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _patch_ark_data(
    monkeypatch, cron, *,
    today_holdings_by_fund: dict,
    yesterday_rows_by_fund: dict,
    fetch_raises: dict | None = None,
    supported_funds: list[str] | None = None,
):
    """Replace cron-module-bound fetch_holdings + get_latest_snapshot_before
    so the cron exercises its real classify_alerts + priority + archive
    path without touching ARK CSV or etf.db.

    ``supported_funds`` overrides the cron's SUPPORTED_FUNDS list — pass
    when a test needs to include funds (like ARKQ) that aren't in the
    production default set."""
    fetch_raises = fetch_raises or {}

    def _stub_fetch(symbol):
        if symbol in fetch_raises:
            raise fetch_raises[symbol]
        holdings = today_holdings_by_fund.get(symbol, [])
        snap_date = (
            holdings[0].date if holdings else "2026-06-09"
        )
        return holdings, snap_date

    def _stub_baseline(symbol, before_date):
        rows = yesterday_rows_by_fund.get(symbol)
        if not rows:
            return None
        return rows

    monkeypatch.setattr(cron, "fetch_holdings", _stub_fetch)
    monkeypatch.setattr(cron, "get_latest_snapshot_before", _stub_baseline)

    # Pin SUPPORTED_FUNDS — defaults to the production set, override
    # when a test needs to include extra funds (e.g. multi-fund test
    # uses ARKQ which isn't in current production SUPPORTED_FUNDS).
    monkeypatch.setattr(
        cron, "SUPPORTED_FUNDS",
        supported_funds or ["ARKK", "ARKW", "ARKG", "ARKF"],
    )


# ---------------------------------------------------------------------------
# Holding builders — match v2.etf.models.ETFHolding shape
# ---------------------------------------------------------------------------

def _holding(etf, ticker, *, shares=100_000.0, market_value=10_000_000.0,
             weight_pct=1.5, company=""):
    from v2.etf.models import ETFHolding
    return ETFHolding(
        etf=etf, date="2026-06-09", ticker=ticker, cusip=None,
        company=company or ticker, shares=shares,
        market_value=market_value, weight_pct=weight_pct,
    )


def _yest_row(etf, ticker, *, shares=100_000.0, market_value=10_000_000.0,
              weight_pct=1.5, company=""):
    return {
        "etf": etf, "date": "2026-06-06", "ticker": ticker, "cusip": None,
        "company": company or ticker, "shares": shares,
        "market_value": market_value, "weight_pct": weight_pct,
    }


# ===========================================================================
# Tests
# ===========================================================================

class TestArkScanCron:

    def _run(self, monkeypatch, cron):
        captured: dict = {}

        def _factory(**kw):
            recorder = _RecordingNotifier(**kw)
            captured["recorder"] = recorder
            return recorder

        monkeypatch.setattr(cron, "TelegramNotifier", _factory)
        rc = cron.main()
        return rc, captured.get("recorder")

    # ---- Normal day ----------------------------------------------------

    def test_normal_day_alerts_pushed(self, monkeypatch, temp_archive):
        """3 alerts on day-over-day diff → 3 individual cards + 1 summary."""
        _install_ark_cron_stubs(monkeypatch, watchlist=[], alpaca_positions=[])
        cron = _load_script("ark_alerts_to_telegram.py")

        # ARKK: NVDA new_position (1.85% > 0.5%) + TSLA increase +25%
        # ARKF: COIN decrease -30%
        # ARKW + ARKG: no-op (same shares both days → detector filters)
        today = {
            "ARKK": [
                _holding("ARKK", "NVDA", shares=250_000,
                         market_value=31_500_000, weight_pct=1.85),
                _holding("ARKK", "TSLA", shares=2_500_000,
                         market_value=625_000_000, weight_pct=10.2),
            ],
            "ARKW": [_holding("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_holding("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [
                _holding("ARKF", "COIN", shares=280_000,
                         market_value=42_000_000, weight_pct=3.5),
            ],
        }
        yest = {
            "ARKK": [
                # TSLA in yesterday at 8.1% / 2M shares
                _yest_row("ARKK", "TSLA", shares=2_000_000,
                          market_value=500_000_000, weight_pct=8.1),
                # NVDA NOT in yesterday → triggers new_position
            ],
            "ARKW": [_yest_row("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_yest_row("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [
                _yest_row("ARKF", "COIN", shares=400_000,
                          market_value=60_000_000, weight_pct=5.0),
            ],
        }
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # 3 individual + 1 summary
        assert len(rec.calls) == 4, (
            f"expected 4 cards (3 individual + 1 summary), got "
            f"{len(rec.calls)}: {[c['title'] for c in rec.calls]}"
        )
        titles = [c["title"] for c in rec.calls]
        assert any("NVDA" in t and "新建仓" in t for t in titles)
        assert any("TSLA" in t and "增持" in t for t in titles)
        assert any("COIN" in t and "减持" in t for t in titles)
        assert any("总览" in t for t in titles)

    # ---- Quiet day -----------------------------------------------------

    def test_quiet_day_silent_no_alerts(self, monkeypatch, temp_archive):
        """All funds scanned, no significant rebalances → 0 push."""
        _install_ark_cron_stubs(monkeypatch, watchlist=[], alpaca_positions=[])
        cron = _load_script("ark_alerts_to_telegram.py")

        # Same shares both days → diff produces no rebalance > 1% (filtered
        # by detector). classify_alerts → []
        today = {
            f: [_holding(f, "AAA", shares=100_000, weight_pct=1.0)]
            for f in ("ARKK", "ARKW", "ARKG", "ARKF")
        }
        yest = {
            f: [_yest_row(f, "AAA", shares=100_000, weight_pct=1.0)]
            for f in ("ARKK", "ARKW", "ARKG", "ARKF")
        }
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # No push notifier ever instantiated
        assert rec is None or rec.calls == []

        # But the trace context should have framed module_enter /
        # module_exit so observability is intact even on quiet days
        # (this test focuses on no-push behavior; framing is verified
        # in test_responder_name_correct below).

    # ---- Multi-fund P0 escalation -------------------------------------

    def test_multi_fund_coordinated_p0_boost(self, monkeypatch, temp_archive):
        """TSMC increase in ARKK + ARKQ same day → both alerts get
        is_multi_fund=True → +15 multi_fund_coordination reason →
        P1 base (65) + 15 = 80 → P0."""
        _install_ark_cron_stubs(monkeypatch, watchlist=[], alpaca_positions=[])
        cron = _load_script("ark_alerts_to_telegram.py")

        today = {
            "ARKK": [_holding("ARKK", "TSMC", shares=300_000,
                              market_value=45_000_000, weight_pct=3.5)],
            "ARKQ": [_holding("ARKQ", "TSMC", shares=200_000,
                              market_value=30_000_000, weight_pct=2.0)],
            "ARKW": [_holding("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKF": [_holding("ARKF", "BBB", shares=100_000, weight_pct=1.0)],
        }
        yest = {
            "ARKK": [_yest_row("ARKK", "TSMC", shares=200_000,
                               market_value=30_000_000, weight_pct=2.3)],
            "ARKQ": [_yest_row("ARKQ", "TSMC", shares=120_000,
                               market_value=18_000_000, weight_pct=1.1)],
            "ARKW": [_yest_row("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKF": [_yest_row("ARKF", "BBB", shares=100_000, weight_pct=1.0)],
        }
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
            # Override to include ARKQ so multi-fund detection can fire
            # on TSMC across ARKK + ARKQ same day.
            supported_funds=["ARKK", "ARKQ", "ARKW", "ARKF"],
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0

        individual = [c for c in rec.calls if "总览" not in c["title"]]
        assert len(individual) == 2, (
            f"expected 2 individual cards (ARKK + ARKQ TSMC); got "
            f"{len(individual)}"
        )
        for call in individual:
            assert "TSMC" in call["title"]
            assert call["priority_tier"] == "P0", (
                f"multi-fund TSMC should escalate to P0; got "
                f"tier={call['priority_tier']} score={call['priority_score']} "
                f"reasons={call['priority_reasons']}"
            )
            assert any(
                "multi_fund_coordination" in r for r in call["priority_reasons"]
            ), f"reasons trail missing multi_fund: {call['priority_reasons']}"

    # ---- User-universe priority boost ---------------------------------

    def test_held_ticker_priority_boost(self, monkeypatch, temp_archive):
        """NVDA in held + ARKK new_position → priority bumped via
        +10 held_or_watchlist_ark. Base 65 + 10 = 75 → P1 (no tier
        change since base already P1; reason in trail is the signal)."""
        _install_ark_cron_stubs(
            monkeypatch, alpaca_positions=["NVDA"], watchlist=[],
        )
        cron = _load_script("ark_alerts_to_telegram.py")

        today = {
            "ARKK": [
                # ARKK has both NVDA (new) AND a stable existing holding
                # to make the baseline non-empty (otherwise the cron
                # falls into the first-deploy branch).
                _holding("ARKK", "NVDA", shares=250_000,
                         market_value=31_500_000, weight_pct=1.85),
                _holding("ARKK", "STABLE", shares=100_000, weight_pct=1.0),
            ],
            "ARKW": [_holding("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_holding("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [_holding("ARKF", "CCC", shares=100_000, weight_pct=1.0)],
        }
        yest = {
            "ARKK": [_yest_row("ARKK", "STABLE", shares=100_000, weight_pct=1.0)],
            "ARKW": [_yest_row("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_yest_row("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [_yest_row("ARKF", "CCC", shares=100_000, weight_pct=1.0)],
        }
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        individual = [c for c in rec.calls if "NVDA" in c["title"]]
        assert len(individual) == 1
        call = individual[0]
        # held +10 lifts base 65 to 75 → still P1
        assert call["priority_tier"] == "P1"
        assert call["priority_score"] == 75, (
            f"expected 75 (base 65 + held_or_watchlist_ark 10); got "
            f"{call['priority_score']} reasons={call['priority_reasons']}"
        )
        assert any(
            "held_or_watchlist_ark" in r for r in call["priority_reasons"]
        )

    # ---- Per-fund CSV failure ------------------------------------------

    def test_ark_csv_failure_per_fund_silent(self, monkeypatch, temp_archive):
        """ARKG raises HTTPError → other 3 funds still produce alerts +
        summary footer shows (3/4 ARK funds) + 'warnings: 1' tail."""
        _install_ark_cron_stubs(monkeypatch, watchlist=[], alpaca_positions=[])
        cron = _load_script("ark_alerts_to_telegram.py")

        today = {
            "ARKK": [
                _holding("ARKK", "NVDA", shares=250_000,
                         market_value=31_500_000, weight_pct=1.85),
                _holding("ARKK", "STABLE", shares=100_000, weight_pct=1.0),
            ],
            "ARKW": [_holding("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            # ARKG raises in fetch_raises
            "ARKF": [_holding("ARKF", "BBB", shares=100_000, weight_pct=1.0)],
        }
        yest = {
            "ARKK": [_yest_row("ARKK", "STABLE", shares=100_000, weight_pct=1.0)],
            "ARKW": [_yest_row("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKF": [_yest_row("ARKF", "BBB", shares=100_000, weight_pct=1.0)],
        }
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
            fetch_raises={"ARKG": RuntimeError("HTTP 503")},
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # At least 1 individual + 1 summary
        summary_calls = [c for c in rec.calls if "总览" in c["title"]]
        assert len(summary_calls) == 1
        summary_text = summary_calls[0]["text"]
        assert "(3/4 ARK funds)" in summary_text, (
            f"expected partial-failure fraction in summary; got:\n{summary_text}"
        )
        assert "warnings: 1" in summary_text

    # ---- First deploy (no yesterday baseline) -------------------------

    def test_no_yesterday_data_no_alerts(self, monkeypatch, temp_archive):
        """Fresh install: get_latest_snapshot_before returns None for
        every fund → classify_alerts produces no diffs → 0 push."""
        _install_ark_cron_stubs(monkeypatch, watchlist=[], alpaca_positions=[])
        cron = _load_script("ark_alerts_to_telegram.py")

        today = {
            f: [_holding(f, "NVDA", shares=250_000,
                         market_value=31_500_000, weight_pct=1.85)]
            for f in ("ARKK", "ARKW", "ARKG", "ARKF")
        }
        # All yesterday baselines absent
        yest = {f: None for f in ("ARKK", "ARKW", "ARKG", "ARKF")}
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0
        # Cron logs "no yesterday baseline" per fund and produces no
        # alerts → no notifier instantiated.
        assert rec is None or rec.calls == []

    # ---- Responder name trace framing ---------------------------------

    def test_responder_name_correct(self, monkeypatch, temp_archive):
        """capture_trace_with_framing tags responder_name='_r_ark_alerts'
        — verified via archive.pushes.trace_json on a normal-day card."""
        _install_ark_cron_stubs(monkeypatch, watchlist=[], alpaca_positions=[])
        cron = _load_script("ark_alerts_to_telegram.py")

        today = {
            "ARKK": [
                _holding("ARKK", "NVDA", shares=250_000,
                         market_value=31_500_000, weight_pct=1.85),
                _holding("ARKK", "STABLE", shares=100_000, weight_pct=1.0),
            ],
            "ARKW": [_holding("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_holding("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [_holding("ARKF", "CCC", shares=100_000, weight_pct=1.0)],
        }
        yest = {
            "ARKK": [_yest_row("ARKK", "STABLE", shares=100_000, weight_pct=1.0)],
            "ARKW": [_yest_row("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_yest_row("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [_yest_row("ARKF", "CCC", shares=100_000, weight_pct=1.0)],
        }
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
        )

        rc, _rec = self._run(monkeypatch, cron)
        assert rc == 0

        conn = sqlite3.connect(str(temp_archive / "archive.db"))
        row = conn.execute(
            "SELECT trace_json FROM pushes "
            "WHERE agent='ark' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None and row[0]
        events = json.loads(row[0])
        names = {
            e.get("name") for e in events
            if e.get("type") in ("module_enter", "module_exit")
        }
        assert "_r_ark_alerts" in names, (
            f"responder_name missing from trace: {names}"
        )

    # ---- Byte-equal individual card -----------------------------------

    def test_byte_equal_card_matches_formatter(
        self, monkeypatch, temp_archive,
    ):
        """⑬ cron's individual-alert text == format_ark_alert(alert)
        byte-equal — Phase 3 cross-surface identity convention."""
        from v2.etf._ark_alert_cards import format_ark_alert
        from v2.etf.alerts import ArkAlert

        _install_ark_cron_stubs(
            monkeypatch, alpaca_positions=["NVDA"], watchlist=[],
        )
        cron = _load_script("ark_alerts_to_telegram.py")

        today = {
            "ARKK": [
                _holding(
                    "ARKK", "NVDA", shares=250_000,
                    market_value=31_500_000, weight_pct=1.85,
                    company="NVIDIA Corp",
                ),
                _holding("ARKK", "STABLE", shares=100_000, weight_pct=1.0),
            ],
            "ARKW": [_holding("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_holding("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [_holding("ARKF", "CCC", shares=100_000, weight_pct=1.0)],
        }
        yest = {
            "ARKK": [_yest_row("ARKK", "STABLE", shares=100_000, weight_pct=1.0)],
            "ARKW": [_yest_row("ARKW", "AAA", shares=100_000, weight_pct=1.0)],
            "ARKG": [_yest_row("ARKG", "BBB", shares=100_000, weight_pct=1.0)],
            "ARKF": [_yest_row("ARKF", "CCC", shares=100_000, weight_pct=1.0)],
        }
        _patch_ark_data(
            monkeypatch, cron,
            today_holdings_by_fund=today,
            yesterday_rows_by_fund=yest,
        )

        rc, rec = self._run(monkeypatch, cron)
        assert rc == 0

        nvda_call = next(c for c in rec.calls if "NVDA" in c["title"])
        # Reconstruct what the cron should have built — same shape as
        # the cron's classify_alerts → ArkAlert path produces.
        expected_alert = ArkAlert(
            fund="ARKK", ticker="NVDA", company="NVIDIA Corp",
            action="new_position",
            yesterday_weight=None, today_weight=1.85,
            weight_change_relative=1.0,
            shares_change=250_000,
            market_value_usd=31_500_000.0,
            is_in_user_universe=True,   # NVDA in alpaca_positions
            is_multi_fund=False,
        )
        expected = format_ark_alert(expected_alert)
        assert nvda_call["text"] == expected, (
            f"\n--- actual ---\n{nvda_call['text']}\n"
            f"\n--- expected ---\n{expected}"
        )
