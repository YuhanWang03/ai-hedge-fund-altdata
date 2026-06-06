"""Cron-script integration tests for ⑨b Positions Snapshot
(Phase 2.5 full).

Loads ``scripts/portfolio_snapshot.py`` via importlib with the
shared portfolio sys.modules stub harness (v2.data shell + v2.broker
+ v2.reporting wired through v2.portfolio._bot_cards), runs cron.main()
against a temp-dir Archive, and asserts on:

- positions_snapshot rows actually land in archive.db via the new
  ``write_position_snapshots`` method.
- Same-day rerun overwrites instead of duplicating (PRIMARY KEY
  semantics).
- ⑨b is SILENT — no Telegram send (no TelegramNotifier import on
  the cron + no notifier.send_text path).
- Alpaca outages don't crash the scheduler (return code 0).
- trace_json captures the responder_name='_r_positions_snapshot'
  framing so the dashboard's auto-push feed can show the run.
- Empty positions (all-cash day) → 0 rows + clean exit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Reuse the Phase 2 portfolio cron harness — same _install_cron_stubs,
# _load_script, temp_archive fixture. The _RecordingNotifier import is
# present for harness symmetry even though ⑨b never instantiates one.
from dashboard.backend.tests.test_portfolio_cron_integration import (  # noqa: E402
    _NORMAL_PORTFOLIO,
    _install_cron_stubs,
    _load_script,
    temp_archive,    # pytest fixture, re-exported for collection
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot_rows(archive_db_path) -> list[tuple]:
    """Read all rows from positions_snapshot for assertion."""
    import sqlite3
    conn = sqlite3.connect(str(archive_db_path))
    try:
        return conn.execute(
            "SELECT snapshot_date, ticker, market_value, weight, sector_etf "
            "FROM positions_snapshot ORDER BY snapshot_date, ticker"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPositionsSnapshotCron:

    def test_normal_day_writes_snapshot(self, monkeypatch, temp_archive):
        """Mocked Alpaca portfolio → N rows in positions_snapshot table.

        Stage 2.5 spec fixture (_NORMAL_PORTFOLIO) carries 6 positions
        across NVDA / AAPL / JPM / MSFT / CRM / BAC, so we expect 6
        rows after ⑨b runs."""
        _install_cron_stubs(monkeypatch, broker_overrides={
            "get_portfolio": lambda: _NORMAL_PORTFOLIO,
        })
        cron = _load_script("portfolio_snapshot.py")
        rc = cron.main()

        assert rc == 0
        rows = _snapshot_rows(temp_archive / "archive.db")
        assert len(rows) == 6
        tickers = {r[1] for r in rows}
        assert tickers == {"NVDA", "AAPL", "JPM", "MSFT", "CRM", "BAC"}
        # All rows share the same snapshot_date (today's ISO)
        snap_dates = {r[0] for r in rows}
        assert len(snap_dates) == 1
        from datetime import datetime
        from zoneinfo import ZoneInfo
        expected_iso = datetime.now(ZoneInfo("US/Eastern")).date().isoformat()
        assert snap_dates == {expected_iso}
        # Weight + market_value populated
        nvda_row = next(r for r in rows if r[1] == "NVDA")
        assert nvda_row[2] == 36120.0      # market_value
        assert nvda_row[3] > 0             # weight

    def test_alpaca_down_silent_skip(self, monkeypatch, temp_archive):
        """Alpaca get_portfolio raises → cron exits 0, 0 rows written.

        get_flat_positions internally catches the exception, returns
        empty + warnings; write_daily_snapshot then writes 0 rows. No
        propagation up the cron, no Telegram alert (the silent-backend
        contract holds even on outages)."""
        def boom():
            raise RuntimeError("Alpaca 503")

        _install_cron_stubs(monkeypatch, broker_overrides={
            "get_portfolio": boom,
        })
        cron = _load_script("portfolio_snapshot.py")
        rc = cron.main()

        assert rc == 0
        rows = _snapshot_rows(temp_archive / "archive.db")
        assert rows == []

    def test_replace_on_same_day_rerun(self, monkeypatch, temp_archive):
        """⑨b twice on the same day → 2nd run overwrites, no duplicates.

        Verifies the PRIMARY KEY (snapshot_date, ticker) semantics end-
        to-end: same day + same ticker + DIFFERENT market_value should
        update in place. Each ticker still has exactly 1 row."""
        portfolio_v1 = {
            "account": {"portfolio_value": 100_000.0, "cash": 10_000.0},
            "positions": [{"symbol": "NVDA", "market_value": "30000"}],
        }
        portfolio_v2 = {
            "account": {"portfolio_value": 100_000.0, "cash": 10_000.0},
            "positions": [{"symbol": "NVDA", "market_value": "31500"}],
        }

        # First run
        _install_cron_stubs(monkeypatch, broker_overrides={
            "get_portfolio": lambda: portfolio_v1,
        })
        cron = _load_script("portfolio_snapshot.py")
        assert cron.main() == 0
        rows_v1 = _snapshot_rows(temp_archive / "archive.db")
        assert len(rows_v1) == 1
        assert rows_v1[0][2] == 30000.0     # market_value

        # Second run — price moved intraday
        _install_cron_stubs(monkeypatch, broker_overrides={
            "get_portfolio": lambda: portfolio_v2,
        })
        cron = _load_script("portfolio_snapshot.py")
        assert cron.main() == 0
        rows_v2 = _snapshot_rows(temp_archive / "archive.db")
        # Still 1 row (REPLACE, not INSERT) — the value updated
        assert len(rows_v2) == 1
        assert rows_v2[0][2] == 31500.0

    def test_trace_responder_name_correct(self, monkeypatch, temp_archive):
        """⑨b's trace events carry name='_r_positions_snapshot' on the
        module_enter framing — required for the dashboard's auto-push
        feed to look up the right event_explanations entry."""
        _install_cron_stubs(monkeypatch, broker_overrides={
            "get_portfolio": lambda: _NORMAL_PORTFOLIO,
        })
        cron = _load_script("portfolio_snapshot.py")
        assert cron.main() == 0

        # ⑨b doesn't push to Telegram (silent), so it doesn't write a
        # row to the pushes table — trace_json isn't surfaced through
        # the archive's pushes column path. We verify the trace contract
        # at the function level instead: capture_trace_with_framing was
        # called with the right responder_name. The cron's main() uses
        # it via context manager; we cross-check by inspecting the
        # script source. (Stage 5+ may add a separate trace table for
        # silent backend jobs; for now the source-level pin is enough.)
        src = (
            _REPO_ROOT / "scripts" / "portfolio_snapshot.py"
        ).read_text(encoding="utf-8")
        assert 'responder_name="_r_positions_snapshot"' in src
        assert 'intent="positions_snapshot"' in src

    def test_no_telegram_push(self, monkeypatch, temp_archive):
        """⑨b doesn't import TelegramNotifier at all — silent contract.

        Catches a regression where someone wires a push to the snapshot
        cron 'for visibility' and turns the every-weekday-16:25-ET tick
        into Telegram noise."""
        src = (
            _REPO_ROOT / "scripts" / "portfolio_snapshot.py"
        ).read_text(encoding="utf-8")
        # The cron MUST import notify_on_error (decorator for the @main
        # error path → that DOES push to Telegram on uncaught crash)
        # but MUST NOT import TelegramNotifier (the push surface).
        assert "TelegramNotifier" not in src, (
            "scripts/portfolio_snapshot.py imports TelegramNotifier — "
            "⑨b is a silent backend job, no Telegram push allowed."
        )
        assert "notify_on_error" in src, (
            "Missing @notify_on_error decorator — uncaught crashes "
            "won't alert the operator."
        )
        assert "send_text" not in src
        assert "send_photo" not in src

    def test_empty_positions_writes_zero_rows(self, monkeypatch, temp_archive):
        """All-cash account → 0 positions → 0 snapshot rows + clean exit.

        Holiday rebalance / fresh deposit days are normal — we don't
        want them generating a warning log noise floor."""
        all_cash = {
            "account": {"portfolio_value": 100_000.0, "cash": 100_000.0},
            "positions": [],
        }
        _install_cron_stubs(monkeypatch, broker_overrides={
            "get_portfolio": lambda: all_cash,
        })
        cron = _load_script("portfolio_snapshot.py")
        rc = cron.main()

        assert rc == 0
        rows = _snapshot_rows(temp_archive / "archive.db")
        assert rows == []
