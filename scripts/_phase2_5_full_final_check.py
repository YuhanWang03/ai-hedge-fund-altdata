"""Phase 2.5 完整版 final verification — run once after deploy, then delete.

Single-run sanity check that Stages 1-3 wired up correctly. Safe to
delete after a successful production run + 1 week of observation
(give ⑩ Friday a chance to actually render the per-position
attribution block once the 5-day window is full).

Five checks:

  1. positions_snapshot table exists in the schema (created by
     Archive.__init__'s _SCHEMA executescript).
  2. ⑨b cron registered (Mon-Fri 16:25 ET) — scheduler builds 19
     jobs, the positions_snapshot job is in the list with the
     correct trigger.
  3. compute_drawdown has today_realtime_value kwarg — the
     signature was extended in Stage 1 to thread the Alpaca
     real-time portfolio value into the drawdown walk; verifies the
     2026-06-05 prod-bug fix is in place.
  4. v2.portfolio.snapshot module exposes the documented public API
     (3 functions + 2 dataclasses + 1 constant).
  5. portfolio_weekly_to_telegram.py wires the attribution path
     end-to-end (reads via read_weekly_window, passes attribution +
     snapshot_days_available into format_portfolio_weekly_card).

Usage:
    poetry run python scripts/_phase2_5_full_final_check.py

Expected: 5/5 ✓ on both sandbox and production. Phase 2.5 完整版 is
self-contained — no production-only deps (no FRED key, no Alpaca
required at check time, no edgartools). The drawdown fix is a pure
Python signature change verifiable via inspect.signature.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
import types
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Sandbox stubs for v2.data / v2.data.models / v2.broker so v2.portfolio
# imports cleanly. Production-only modules; we mirror the test_*
# harness pattern that the rest of the suite uses.
if "v2.data" not in sys.modules or not hasattr(
    sys.modules.get("v2.data"), "CachedFDClient",
):
    _v2_data = types.ModuleType("v2.data")
    _v2_data.__path__ = []
    _v2_data.CachedFDClient = type("CachedFDClient", (), {})
    _v2_data.FDClient = type("FDClient", (), {})
    sys.modules["v2.data"] = _v2_data

if "v2.broker" not in sys.modules:
    _v2_broker = types.ModuleType("v2.broker")
    _v2_broker.AlpacaUnavailable = RuntimeError
    _v2_broker.get_portfolio = lambda: {"positions": []}
    _v2_broker.get_pnl = lambda: {"intraday_pl": 0.0, "intraday_pl_pct": 0.0}
    _v2_broker.get_portfolio_history = lambda **kw: {
        "equity": [], "timestamp": [],
    }
    sys.modules["v2.broker"] = _v2_broker


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. positions_snapshot table exists in schema
# ---------------------------------------------------------------------------

def check_table() -> None:
    """Archive's _SCHEMA executescript must create positions_snapshot
    + the two indexes. Verify by spinning up an in-memory SQLite from
    a temp Archive instance and inspecting sqlite_master."""
    import sqlite3
    import tempfile
    from v2.archive import store as archive_store

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # Point _DB_PATH at a temp file so Archive() doesn't touch
        # the real production DB.
        orig_db = archive_store._DB_PATH
        archive_store._DB_PATH = td_path / "archive.db"
        try:
            from v2.archive import Archive
            Archive("test_check")
            conn = sqlite3.connect(str(archive_store._DB_PATH))
            try:
                tables = {
                    row[0] for row in
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                assert "positions_snapshot" in tables, (
                    f"positions_snapshot table missing — got: {sorted(tables)}"
                )
                cols = {
                    row[1] for row in
                    conn.execute("PRAGMA table_info(positions_snapshot)")
                }
                expected_cols = {
                    "snapshot_date", "ticker", "market_value",
                    "weight", "sector_etf",
                }
                missing = expected_cols - cols
                assert not missing, (
                    f"positions_snapshot missing columns: {missing}"
                )
                indexes = {
                    row[0] for row in
                    conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='positions_snapshot'"
                    )
                }
                # PRIMARY KEY creates an autoindex; our two explicit ones
                # are idx_positions_snapshot_date + idx_positions_snapshot_ticker.
                assert "idx_positions_snapshot_date" in indexes, (
                    f"snapshot_date index missing — got: {sorted(indexes)}"
                )
                assert "idx_positions_snapshot_ticker" in indexes, (
                    f"ticker index missing — got: {sorted(indexes)}"
                )
            finally:
                conn.close()
        finally:
            archive_store._DB_PATH = orig_db

    log.info(
        "  ✓ positions_snapshot table + 5 columns + 2 indexes created "
        "by Archive.__init__",
    )


# ---------------------------------------------------------------------------
# 2. ⑨b cron registered (Mon-Fri 16:25 ET)
# ---------------------------------------------------------------------------

def check_cron() -> None:
    """⑨b job present in scheduler with the right CronTrigger."""
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}

    assert "positions_snapshot" in jobs, (
        f"positions_snapshot job not registered. Got: {sorted(jobs.keys())}"
    )
    assert len(jobs) >= 19, (
        f"expected ≥ 19 scheduler jobs, got {len(jobs)}: {sorted(jobs.keys())}"
    )

    job = jobs["positions_snapshot"]
    tf = {f.name: str(f) for f in job.trigger.fields}
    assert tf.get("hour") == "16", (
        f"⑨b hour expected 16, got {tf.get('hour')!r}"
    )
    assert tf.get("minute") == "25", (
        f"⑨b minute expected 25, got {tf.get('minute')!r}"
    )
    assert "mon-fri" in tf.get("day_of_week", ""), (
        f"⑨b day_of_week expected to contain 'mon-fri'; "
        f"got {tf.get('day_of_week')!r}"
    )

    log.info(
        "  ✓ scheduler builds %d jobs including ⑨b positions_snapshot "
        "(Mon-Fri 16:25 ET); ⑭ Macro Snapshot at 16:30 ET sits 5 min "
        "after for unambiguous serial log timestamps", len(jobs),
    )


# ---------------------------------------------------------------------------
# 3. compute_drawdown has today_realtime_value kwarg
# ---------------------------------------------------------------------------

def check_drawdown_signature() -> None:
    """Stage 1 added a keyword-only today_realtime_value param to
    compute_drawdown. inspect.signature confirms it's in place +
    keyword-only + defaults to None (backward compat with Phase 2)."""
    from v2.portfolio.drawdown import compute_drawdown

    sig = inspect.signature(compute_drawdown)
    params = sig.parameters
    assert "today_realtime_value" in params, (
        f"compute_drawdown is missing today_realtime_value kwarg. "
        f"Got params: {list(params.keys())}"
    )
    p = params["today_realtime_value"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"today_realtime_value should be keyword-only; "
        f"got kind={p.kind}"
    )
    assert p.default is None, (
        f"today_realtime_value default should be None (backward-compat); "
        f"got default={p.default}"
    )

    log.info(
        "  ✓ compute_drawdown(broker, *, today_realtime_value=None) — "
        "keyword-only with None default preserves Phase 2 behaviour"
    )


# ---------------------------------------------------------------------------
# 4. v2.portfolio.snapshot public API
# ---------------------------------------------------------------------------

def check_snapshot_module() -> None:
    """Module exposes 3 functions + 2 dataclasses + 1 constant via
    __all__."""
    from v2.portfolio import snapshot as snap_mod

    expected = {
        "AttributionItem", "PositionSnapshot", "WEEKLY_MIN_DAYS",
        "compute_weekly_attribution", "read_weekly_window",
        "write_daily_snapshot",
    }
    for name in expected:
        assert hasattr(snap_mod, name), (
            f"v2.portfolio.snapshot missing {name}"
        )
        assert name in snap_mod.__all__, (
            f"{name} not in v2.portfolio.snapshot.__all__"
        )

    # Pin the gating threshold (⑩ card "归因数据累积中 (N/5)" depends
    # on this exact value — a drift here would silently break the
    # byte-equal pin in test_weekly_card_insufficient_snapshots).
    assert snap_mod.WEEKLY_MIN_DAYS == 5, (
        f"WEEKLY_MIN_DAYS drifted to {snap_mod.WEEKLY_MIN_DAYS}; "
        f"byte-equal pin expects 5"
    )

    log.info(
        "  ✓ v2.portfolio.snapshot — 6 public names exposed via __all__ "
        "(3 fn + 2 dataclass + WEEKLY_MIN_DAYS=5)"
    )


# ---------------------------------------------------------------------------
# 5. ⑩ cron wires the attribution path end-to-end
# ---------------------------------------------------------------------------

def check_pipeline_integration() -> None:
    """portfolio_weekly_to_telegram.py imports read_weekly_window +
    compute_weekly_attribution, computes snapshot_days_available, and
    passes both to format_portfolio_weekly_card. Source-level pin
    catches a regression where someone reverts the wiring to use
    None defaults.
    """
    src = (
        _REPO_ROOT / "scripts" / "portfolio_weekly_to_telegram.py"
    ).read_text(encoding="utf-8")

    required_imports = (
        "read_weekly_window",
        "compute_weekly_attribution",
    )
    for name in required_imports:
        assert name in src, (
            f"portfolio_weekly_to_telegram.py missing import of {name}"
        )

    # Function-call shape: must actually call them, not just import
    assert "read_weekly_window(archive" in src, (
        "portfolio_weekly_to_telegram.py imports read_weekly_window "
        "but doesn't appear to call it — wiring regression?"
    )
    assert "compute_weekly_attribution(" in src, (
        "portfolio_weekly_to_telegram.py imports compute_weekly_attribution "
        "but doesn't appear to call it — wiring regression?"
    )
    # Must pass BOTH attribution + snapshot_days_available to the
    # formatter — 3-state gating logic in format_portfolio_weekly_card
    # needs both to render correctly.
    assert "attribution=attribution" in src, (
        "portfolio_weekly_to_telegram.py doesn't pass "
        "attribution= into format_portfolio_weekly_card"
    )
    assert "snapshot_days_available=" in src, (
        "portfolio_weekly_to_telegram.py doesn't pass "
        "snapshot_days_available= into format_portfolio_weekly_card"
    )

    log.info(
        "  ✓ ⑩ cron wires read_weekly_window + compute_weekly_attribution "
        "+ passes both kwargs to format_portfolio_weekly_card"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("positions_snapshot table + 2 indexes exist",            check_table),
        ("⑨b cron registered (Mon-Fri 16:25 ET)",                 check_cron),
        ("compute_drawdown has today_realtime_value kwarg",       check_drawdown_signature),
        ("v2.portfolio.snapshot public API (3 fn + 2 cls + const)", check_snapshot_module),
        ("⑩ cron wires attribution end-to-end",                   check_pipeline_integration),
    ]

    log.info("Phase 2.5 完整版 — Per-position Attribution + Drawdown Realtime")
    log.info("=" * 64)
    failed: list[str] = []
    for label, fn in checks:
        log.info("[%s]", label)
        try:
            fn()
        except Exception as exc:
            log.error("  ✗ FAILED: %s", exc)
            failed.append(label)

    log.info("")
    if failed:
        log.error("❌ %d check(s) failed: %s", len(failed), failed)
        return 1
    log.info("✅ Phase 2.5 完整版 全部就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
