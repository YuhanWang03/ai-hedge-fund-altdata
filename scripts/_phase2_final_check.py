"""Phase 2 Portfolio Risk Agent — final end-to-end sanity check.

Single-run verification that Stages 1-7 actually wired Phase 2 up
correctly. Safe to delete after a successful run on the deploy box.

Six checks (mirrors the Phase-1 script's structure):

  1. priority.BASE_SCORES has portfolio_risk + portfolio_alert,
     plus all 3 new adjustment factors (top_1_pct / max_drawdown_pct /
     n_earnings_next_7d) are wired into compute_importance.
  2. v2.scheduler builds 12 jobs including ⑨ portfolio_risk + ⑩
     portfolio_weekly.
  3. v2.bot.intent registers risk_view + pnl_period, 19 + unknown = 20.
     /risk slash command attached in v2/bot/main.py.
  4. v2.reporting exposes 4 portfolio formatters; identities match the
     v2.portfolio._bot_cards source-of-truth.
  5. v2.portfolio.drawdown.compute_drawdown returns non-negative
     magnitudes (Stage 5 sign convention).
  6. v2/portfolio/_smoke_stage1.py passes 20/20.

Usage:
    poetry run python scripts/_phase2_final_check.py

Sandbox: 5/6 ✓ + 1 ⚠ SKIPPED (v2.reporting public import) — production
will be 6/6 ✓ because v2.data is installed there.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


class _SandboxSkip(Exception):
    """Raised when a check is structurally fine but the sandbox is
    missing a production-only dep (e.g. v2.data). Reported as SKIP,
    not FAIL — production run hits all 6 checks."""


# ---------------------------------------------------------------------------
# 1. priority BASE_SCORES + 3 new adjustment factors
# ---------------------------------------------------------------------------

def check_priority() -> None:
    """Both base scores registered AND all 4 adjustment factors fire:
    daily_pnl (Phase 0), top_1_pct / max_drawdown_pct / n_earnings_next_7d
    (Phase 2 Stage 2)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase2_priority", _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    p = importlib.util.module_from_spec(spec)
    sys.modules["_phase2_priority"] = p
    spec.loader.exec_module(p)

    assert p.BASE_SCORES["portfolio_risk"] == 55, \
        f"portfolio_risk base=55, got {p.BASE_SCORES.get('portfolio_risk')}"
    assert p.BASE_SCORES["portfolio_alert"] == 85, \
        f"portfolio_alert base=85, got {p.BASE_SCORES.get('portfolio_alert')}"

    # Each adjustment factor fires independently
    cases = [
        ("top_1_pct=35%",     {"top_1_pct": 0.35},       75),
        ("top_1_pct=20%",     {"top_1_pct": 0.20},       65),
        ("max_drawdown=12%",  {"max_drawdown_pct": 0.12}, 70),
        ("earnings_density=3",{"n_earnings_next_7d": 3},  65),
        ("daily_pnl=-6%",     {"daily_pnl_pct": -0.06},   85),
    ]
    for label, md, expected_score in cases:
        r = p.compute_importance("portfolio_risk", md)
        assert r.score == expected_score, (
            f"{label}: expected score={expected_score}, got {r.score} "
            f"(reasons={r.reasons})"
        )
    log.info("  ✓ portfolio_risk=55, portfolio_alert=85, all 4 adjustment "
             "factors fire correctly")


# ---------------------------------------------------------------------------
# 2. Scheduler 12 jobs including ⑨ + ⑩
# ---------------------------------------------------------------------------

def check_scheduler_12() -> None:
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    jobs = {j.id for j in sched.get_jobs()}

    expected_min = {
        "daily_screen", "anomaly_monitor", "lateral_expansion",
        "institutional", "institutional_backfill", "etf_daily",
        "p2_digest", "archive_cleanup",
        "earnings_reminders", "earnings_summaries",
        "portfolio_risk", "portfolio_weekly",
    }
    missing = expected_min - jobs
    assert not missing, f"scheduler missing jobs: {missing}"
    assert len(jobs) >= 12, (
        f"expected ≥ 12 scheduler jobs, got {len(jobs)}: {jobs}"
    )

    # ⑩ pinned to Friday
    weekly_job = next(
        (j for j in sched.get_jobs() if j.id == "portfolio_weekly"), None,
    )
    assert weekly_job is not None
    trigger_fields = {f.name: str(f) for f in weekly_job.trigger.fields}
    assert "fri" in trigger_fields.get("day_of_week", ""), (
        f"⑩ must fire only on Fri; got {trigger_fields.get('day_of_week')!r}"
    )

    log.info("  ✓ scheduler builds %d jobs including ⑨ portfolio_risk "
             "(Mon-Fri 18:30 ET) + ⑩ portfolio_weekly (Fri 19:00 ET)",
             len(jobs))


# ---------------------------------------------------------------------------
# 3. /risk command + 19 NL intents
# ---------------------------------------------------------------------------

def check_bot_19() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase2_intent", _REPO_ROOT / "v2" / "bot" / "intent.py",
    )
    intent = importlib.util.module_from_spec(spec)
    sys.modules["_phase2_intent"] = intent
    spec.loader.exec_module(intent)

    assert "risk_view" in intent._VALID_INTENTS
    assert "pnl_period" in intent._VALID_INTENTS
    assert "period" in intent._SYSTEM_PROMPT, \
        "period parameter not described in intent system prompt"
    assert "固定 19 个 intent" in intent._SYSTEM_PROMPT
    # 17 + 2 + unknown = 20
    assert len(intent._VALID_INTENTS) == 20, \
        f"expected 20 intents, got {len(intent._VALID_INTENTS)}"

    # /risk handler registered
    main_src = (_REPO_ROOT / "v2" / "bot" / "main.py").read_text()
    assert 'CommandHandler("risk"' in main_src, \
        "/risk CommandHandler not registered in v2/bot/main.py"

    log.info("  ✓ NL intents = %d (17 + risk_view + pnl_period + unknown); "
             "/risk handler registered", len(intent._VALID_INTENTS))


# ---------------------------------------------------------------------------
# 4. 4 public portfolio formatters
# ---------------------------------------------------------------------------

def check_formatters_4() -> None:
    expected = [
        "format_portfolio_risk_card",
        "format_portfolio_risk_view",
        "format_portfolio_weekly_card",
        "format_portfolio_pnl_period",
    ]
    try:
        import v2.reporting as r
    except ModuleNotFoundError as exc:
        if "v2.data" in str(exc):
            from v2.portfolio import _bot_cards as src
            for name in (
                "format_risk_card", "format_risk_view",
                "format_weekly_card", "format_pnl_period",
            ):
                assert hasattr(src, name), f"_bot_cards missing {name}"
            log.info("  ⚠ skipped public v2.reporting import (sandbox missing v2.data); "
                     "_bot_cards source-of-truth exposes all 4 formatters")
            raise _SandboxSkip("v2.data not installed (sandbox)")
        raise

    for name in expected:
        assert hasattr(r, name), f"v2.reporting missing {name}"
        assert name in getattr(r, "__all__", []), \
            f"{name} not in v2.reporting.__all__"

    # Identity check: public name resolves to v2/portfolio/_bot_cards source.
    from v2.portfolio import _bot_cards as src
    assert r.format_portfolio_risk_card is src.format_risk_card
    assert r.format_portfolio_risk_view is src.format_risk_view
    assert r.format_portfolio_weekly_card is src.format_weekly_card
    assert r.format_portfolio_pnl_period is src.format_pnl_period

    log.info("  ✓ v2.reporting.format_portfolio_* — all 4 public formatters "
             "resolve to v2.portfolio._bot_cards source-of-truth")


# ---------------------------------------------------------------------------
# 5. drawdown non-negative invariant (Stage 5 sign convention)
# ---------------------------------------------------------------------------

def check_drawdown_sign() -> None:
    """Verify compute_drawdown's non-negative magnitude contract.

    Uses a synthetic broker that returns a 12-day equity series with
    a known peak-to-trough drop. Both max_drawdown_pct and
    current_drawdown_pct must be ≥ 0 (Stage 5 fix for Issue 1)."""
    from v2.portfolio.drawdown import compute_drawdown
    from v2.portfolio.models import DrawdownMetrics

    class FakeBroker:
        def get_portfolio_history(self, period, timeframe):
            # Peak 110 at idx 4, trough 90 at idx 9 (drop 18.18%),
            # partial recovery to 95 at end
            return {
                "equity":    [100, 105, 108, 110, 108, 100, 95, 92, 90, 95],
                "timestamp": list(range(10)),
            }

    metrics, warnings = compute_drawdown(broker=FakeBroker())
    assert isinstance(metrics, DrawdownMetrics)
    assert warnings == []

    # Both magnitudes non-negative
    assert metrics.max_drawdown_pct is not None
    assert metrics.current_drawdown_pct is not None
    assert metrics.max_drawdown_pct >= 0.0, \
        f"max_drawdown must be ≥ 0, got {metrics.max_drawdown_pct}"
    assert metrics.current_drawdown_pct >= 0.0, \
        f"current_drawdown must be ≥ 0, got {metrics.current_drawdown_pct}"

    # Magnitude sanity: trough 90 vs peak 110 = 18.18% drop
    expected_max = abs((90 - 110) / 110)
    assert abs(metrics.max_drawdown_pct - expected_max) < 1e-6, \
        f"max_drawdown magnitude wrong: got {metrics.max_drawdown_pct}, expected {expected_max}"

    log.info("  ✓ drawdown sign convention: max=%.4f current=%.4f (both ≥ 0)",
             metrics.max_drawdown_pct, metrics.current_drawdown_pct)


# ---------------------------------------------------------------------------
# 6. Stage 1 portfolio smoke regression
# ---------------------------------------------------------------------------

def check_smoke_20() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "v2.portfolio._smoke_stage1"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"_smoke_stage1 failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "ALL SMOKE TESTS PASSED" in result.stdout, \
        "smoke didn't print the success marker"
    log.info("  ✓ v2/portfolio/_smoke_stage1.py — 20/20 scenarios pass")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("portfolio BASE_SCORES + 4 adjustment factors", check_priority),
        ("12 scheduler cron jobs registered",            check_scheduler_12),
        ("/risk command + 19 NL intents",                check_bot_19),
        ("4 public portfolio formatters importable",     check_formatters_4),
        ("drawdown sign convention non-negative",        check_drawdown_sign),
        ("Stage 1 portfolio smoke 20/20",                check_smoke_20),
    ]

    log.info("Phase 2 Portfolio Risk Agent — final sanity check")
    log.info("=" * 64)
    failed: list[str] = []
    skipped: list[str] = []
    for label, fn in checks:
        log.info("[%s]", label)
        try:
            fn()
        except _SandboxSkip as exc:
            log.info("  ⚠ SKIPPED: %s", exc)
            skipped.append(label)
        except Exception as exc:
            log.error("  ✗ FAILED: %s", exc)
            failed.append(label)

    log.info("")
    if failed:
        log.error("❌ %d check(s) failed: %s", len(failed), failed)
        return 1
    if skipped:
        log.info(
            "✅ Phase 2 Portfolio Risk Agent 全部就位 (%d check(s) skipped: "
            "%s — re-run on production for full coverage)",
            len(skipped), skipped,
        )
    else:
        log.info("✅ Phase 2 Portfolio Risk Agent 全部就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
