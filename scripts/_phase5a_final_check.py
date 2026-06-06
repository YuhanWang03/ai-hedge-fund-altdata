"""Phase 5a final verification — run once after deploy, then delete.

Single-run sanity check that Stages 1-4 wired up correctly. Safe to
delete after a successful production run + 1 week of observation
(give ⑬ Friday a chance to fire its first real-data alerts).

Six checks (sandbox expects 6/6 ✓ — Phase 5a has no v2.data hot
path; the reused v2.etf/ infrastructure has been battle-tested by
⑤ since Phase 0):

  1. v2.etf.alerts importable + ArkAlert / ArkScanResult / classify_alerts
     public API + frozen-dataclass invariant on ArkAlert.
  2. ⑬ cron registered with the right CronTrigger (Mon-Fri 08:30 ET).
  3. Scheduler builds exactly 21 jobs (20 from Phase 3.5 + 1 new ⑬).
  4. v2.reporting.priority recognises the ark_alert_p0/p1/p2 base
     kinds and applies the 4 adjustments (held_or_watchlist_ark +10,
     multi_fund_coordination +15, large_new_position +10,
     large_liquidation +10).
  5. format_ark_alert + format_ark_summary present on the public
     v2.reporting namespace AND identity-equal to v2.etf._ark_alert_cards.
  6. etf.db.snapshots schema unchanged — Phase 5a's Stage 0 audit
     decision was to reuse the existing table; verify no migration
     drift snuck in.

Usage:
    poetry run python scripts/_phase5a_final_check.py

Expected: 6/6 ✓ on both sandbox and production. No FRED / Alpaca /
ARK CSV / SEC EDGAR network calls are made by any check — every
verification is structural.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import logging
import sys
import types
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Sandbox stubs — match _phase2_5_full / _phase3_5 conventions. ⑬ cron
# transitively imports v2.broker via the universe-resolution path; the
# scheduler module-import pulls v2.reporting which pulls v2.lateral
# which wants v2.data. Stubs keep imports happy on sandbox runs.
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
    sys.modules["v2.broker"] = _v2_broker


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. v2.etf.alerts importable + public API + frozen-dataclass invariant
# ---------------------------------------------------------------------------

def check_alerts_module() -> None:
    """ArkAlert / ArkScanResult / classify_alerts + the frozen invariant
    on ArkAlert (Stage 1 design — multi-fund relabeling uses
    dataclasses.replace rather than mutation)."""
    from v2.etf import alerts

    expected = {"ArkAlert", "ArkScanResult", "classify_alerts"}
    for name in expected:
        assert hasattr(alerts, name), (
            f"v2.etf.alerts missing {name}"
        )
        assert name in alerts.__all__, (
            f"{name} not in v2.etf.alerts.__all__"
        )

    # Frozen invariant — Stage 1 pin
    a = alerts.ArkAlert(
        fund="ARKK", ticker="X", company="X Co",
        action="new_position",
        yesterday_weight=None, today_weight=1.0,
        weight_change_relative=1.0,
        shares_change=1, market_value_usd=1.0,
        is_in_user_universe=False, is_multi_fund=False,
    )
    try:
        a.is_multi_fund = True
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError(
            "ArkAlert should be frozen (Stage 1 invariant) but mutation succeeded"
        )

    # ArkScanResult shape — funds_attempted is the Polish 3 backward-compat
    # field added Stage 3, default empty list keeps old call sites working.
    result = alerts.ArkScanResult(scan_date="2026-06-09", funds_scanned=["ARKK"])
    assert result.alerts == []
    assert result.warnings == []
    assert result.funds_attempted == [], (
        "ArkScanResult.funds_attempted must default to empty list "
        "(backward-compat with pre-Stage-3 callers)"
    )

    log.info(
        "  ✓ v2.etf.alerts: ArkAlert (frozen) + ArkScanResult "
        "(funds_attempted default []) + classify_alerts importable"
    )


# ---------------------------------------------------------------------------
# 2. ⑬ cron registered (Mon-Fri 08:30 ET)
# ---------------------------------------------------------------------------

def check_cron() -> None:
    """⑬ job present in scheduler with CronTrigger(mon-fri 08:30)."""
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}

    assert "ark_alerts" in jobs, (
        f"ark_alerts job not registered. Got: {sorted(jobs.keys())}"
    )

    job = jobs["ark_alerts"]
    tf = {f.name: str(f) for f in job.trigger.fields}
    assert tf.get("hour") == "8", (
        f"⑬ hour expected 8, got {tf.get('hour')!r}"
    )
    assert tf.get("minute") == "30", (
        f"⑬ minute expected 30, got {tf.get('minute')!r}"
    )
    assert "mon-fri" in tf.get("day_of_week", ""), (
        f"⑬ day_of_week expected to contain 'mon-fri'; "
        f"got {tf.get('day_of_week')!r}"
    )

    log.info(
        "  ✓ ⑬ ark_alerts registered at Mon-Fri 08:30 ET — slots "
        "between ⑦ Earnings Reminders (08:00) and ⑮ Macro Release (09:00)"
    )


# ---------------------------------------------------------------------------
# 3. Scheduler builds 21 jobs
# ---------------------------------------------------------------------------

def check_scheduler_21() -> None:
    """Phase 3.5 landed at 20 jobs; Phase 5a adds ⑬ → 21."""
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    n = len(sched.get_jobs())
    assert n == 21, (
        f"scheduler builds {n} jobs, Phase 5a expected exactly 21 "
        "(Phase 3.5 20 + ⑬ ark_alerts)"
    )
    log.info("  ✓ scheduler builds exactly 21 jobs (20 + ⑬)")


# ---------------------------------------------------------------------------
# 4. priority kinds + 4 adjustments
# ---------------------------------------------------------------------------

def check_priority() -> None:
    """compute_importance must:
      - recognise ark_alert_p2 / ark_alert_p1 / ark_alert_p0 base kinds
      - apply +10 held_or_watchlist_ark on is_in_user_universe
      - apply +15 multi_fund_coordination on is_multi_fund
      - apply +10 large_new_position_X.X% when action=new_position and
        today_weight (decimal) ≥ 0.02
      - apply +10 large_liquidation_X.X% when action=liquidated and
        yesterday_weight (decimal) ≥ 0.02
    """
    spec = importlib.util.spec_from_file_location(
        "v2.reporting.priority",
        _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    priority = importlib.util.module_from_spec(spec)
    sys.modules["v2.reporting.priority"] = priority
    spec.loader.exec_module(priority)

    # Base ladder
    for kind, base in (
        ("ark_alert_p2", 55),
        ("ark_alert_p1", 65),
        ("ark_alert_p0", 85),
    ):
        r = priority.compute_importance(kind, {})
        assert r.score == base, (
            f"{kind} base expected {base}, got {r.score}"
        )

    # held_or_watchlist_ark +10 — base 65 + 10 = 75 → P1
    held = priority.compute_importance(
        "ark_alert_p1", {"is_in_user_universe": True},
    )
    assert held.score == 75, f"held boost score expected 75, got {held.score}"
    assert any("held_or_watchlist_ark" in r for r in held.reasons), (
        f"held trail missing reason: {held.reasons}"
    )

    # multi_fund_coordination +15 — base 65 + 15 = 80 → P0
    multi = priority.compute_importance(
        "ark_alert_p1", {"is_multi_fund": True},
    )
    assert multi.score == 80, f"multi-fund score expected 80, got {multi.score}"
    assert multi.tier == "P0", f"multi-fund tier expected P0, got {multi.tier}"
    assert any("multi_fund_coordination" in r for r in multi.reasons)

    # large_new_position +10 — today_weight decimal 0.025 (2.5%) triggers
    lnp = priority.compute_importance(
        "ark_alert_p1",
        {"action": "new_position", "today_weight": 0.025},
    )
    assert any("large_new_position" in r for r in lnp.reasons), (
        f"large_new_position trail missing: {lnp.reasons}"
    )
    assert lnp.score == 75, f"new_position score expected 75 (65+10), got {lnp.score}"

    # large_liquidation +10 — yesterday_weight decimal 0.03 (3%) triggers
    llq = priority.compute_importance(
        "ark_alert_p1",
        {"action": "liquidated", "yesterday_weight": 0.03},
    )
    assert any("large_liquidation" in r for r in llq.reasons), (
        f"large_liquidation trail missing: {llq.reasons}"
    )

    log.info(
        "  ✓ ark_alert_p0/p1/p2 base + 4 adjustments wired "
        "(held +10 / multi_fund +15 / large_new_position +10 / "
        "large_liquidation +10)"
    )


# ---------------------------------------------------------------------------
# 5. format_ark_alert + format_ark_summary public + identity shim
# ---------------------------------------------------------------------------

def check_formatters() -> None:
    """Both formatters callable + identity-equal between v2.reporting
    shim and the source-of-truth in v2.etf._ark_alert_cards. Same
    Phase 1-4 4-layer shim pattern."""
    from v2.etf import _ark_alert_cards as src

    for name in ("format_ark_alert", "format_ark_summary"):
        assert hasattr(src, name), f"v2.etf._ark_alert_cards missing {name}"
        assert name in src.__all__, f"{name} not in __all__"

    # Load the reporting shim directly to bypass v2.reporting's heavy init.
    spec = importlib.util.spec_from_file_location(
        "_phase5a_check_ark_shim",
        _REPO_ROOT / "v2" / "reporting" / "_ark_alert_formatters.py",
    )
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    assert shim.format_ark_alert is src.format_ark_alert, (
        "v2.reporting shim format_ark_alert identity mismatch — Phase 1-4 "
        "4-layer shim guarantees same function object"
    )
    assert shim.format_ark_summary is src.format_ark_summary, (
        "v2.reporting shim format_ark_summary identity mismatch"
    )

    log.info(
        "  ✓ format_ark_alert + format_ark_summary public via "
        "v2/reporting/_ark_alert_formatters → identity-equal to "
        "v2/etf/_ark_alert_cards (4-layer shim consistency)"
    )


# ---------------------------------------------------------------------------
# 6. etf.db.snapshots schema unchanged (Stage 0 audit decision)
# ---------------------------------------------------------------------------

def check_schema_unchanged() -> None:
    """Spin up a temp etf.db via v2.etf.tracker and verify the snapshots
    table has the canonical Phase 0 columns + (etf, date) index. Phase 5a
    explicitly chose NOT to migrate this table — drift here would be a
    regression in the Stage 0 audit decision."""
    import sqlite3
    import tempfile
    from v2.etf import tracker

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        orig_db = tracker._DB_PATH
        tracker._DB_PATH = td_path / "etf.db"
        try:
            # Trigger schema bootstrap by opening a connection
            with tracker._conn():
                pass
            conn = sqlite3.connect(str(tracker._DB_PATH))
            try:
                tables = {
                    row[0] for row in
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                assert "snapshots" in tables, (
                    f"etf.db missing snapshots table — got: {sorted(tables)}"
                )
                cols = {
                    row[1] for row in
                    conn.execute("PRAGMA table_info(snapshots)")
                }
                expected = {
                    "etf", "date", "ticker", "cusip", "company",
                    "shares", "market_value", "weight_pct",
                }
                missing = expected - cols
                assert not missing, (
                    f"snapshots table missing columns: {missing}; "
                    "Phase 5a should NOT have migrated this schema"
                )
                indexes = {
                    row[0] for row in
                    conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='snapshots'"
                    )
                }
                assert "idx_snapshots_etf_date" in indexes, (
                    f"idx_snapshots_etf_date missing — got: {sorted(indexes)}"
                )
            finally:
                conn.close()
        finally:
            tracker._DB_PATH = orig_db

    log.info(
        "  ✓ etf.db.snapshots schema unchanged (Phase 0 baseline + "
        "(etf, date) index) — Stage 0 audit zero-migration decision honored"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("v2.etf.alerts module importable + frozen ArkAlert",     check_alerts_module),
        ("⑬ cron registered (Mon-Fri 08:30 ET)",                  check_cron),
        ("scheduler builds 21 jobs",                                check_scheduler_21),
        ("priority: ark_alert_p0/p1/p2 + 4 adjustments",           check_priority),
        ("format_ark_alert + format_ark_summary public shim",       check_formatters),
        ("etf.db.snapshots schema unchanged",                       check_schema_unchanged),
    ]

    log.info("Phase 5a — ⑬ ARK Rebalance Alerts")
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
    log.info("✅ Phase 5a 全部就位 (6/6)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
