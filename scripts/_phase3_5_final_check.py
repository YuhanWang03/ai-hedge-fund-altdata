"""Phase 3.5 final verification — run once after deploy, then delete.

Single-run sanity check that Stages 1-4 wired up correctly. Safe to
delete after a successful production run + 1 week of observation
(give ⑫b Friday a chance to actually push the weekly insider digest
once + a real earnings filing with a recent 10-Q for ⑧ to render
the new MD&A section).

Six checks (sandbox expects 6/6 ✓ — Phase 3.5 has no v2.data hot
path):

  1. v2.sec.ten_q_parser + v2.sec.insider_digest importable + expose
     the documented public API (TenQDelta + parse/diff + digest +
     default_week_window).
  2. ⑫b cron registered with the right CronTrigger (Fri 19:15 ET).
  3. Scheduler builds exactly 20 jobs (19 from Phase 2.5 full + 1
     new ⑫b).
  4. v2.earnings.pipeline._fetch_recent_ten_q helper exists with the
     test seam signature (fetcher / parser_fn / diff_fn kwargs).
  5. v2.reporting.priority recognises going_concern_in_10q (+20) and
     material_weakness_in_10q (+15) reasons via compute_importance.
  6. EarningsSummary dataclass carries the ten_q_delta field
     (Phase 3.5 Stage 2 added; duck-typed object | None so the
     v2.earnings package doesn't import v2.sec at runtime).

Usage:
    poetry run python scripts/_phase3_5_final_check.py

Expected: 6/6 ✓ on both sandbox and production. Phase 3.5 is fully
sandbox-checkable — no FRED / Alpaca / SEC EDGAR network calls are
made by any check.
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


# Sandbox stubs — same pattern as _phase2_5_full_final_check / Phase 3
# / Phase 4 helpers. v2.scheduler.main pulls v2.reporting transitively
# via the startup notification path; the stubs keep that import happy
# when run outside production.
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
# 1. v2.sec.ten_q_parser + v2.sec.insider_digest importable
# ---------------------------------------------------------------------------

def check_modules() -> None:
    """Both Phase 3.5 modules import cleanly and expose the documented
    public API. ten_q_parser exposes TenQDelta + parse/diff; insider_digest
    exposes WeeklyInsiderSummary + build + window helpers."""
    from v2.sec import ten_q_parser, insider_digest

    expected_ten_q = {"TenQDelta", "parse_ten_q", "diff_ten_q"}
    for name in expected_ten_q:
        assert hasattr(ten_q_parser, name), (
            f"v2.sec.ten_q_parser missing {name}"
        )
        assert name in ten_q_parser.__all__, (
            f"{name} not in v2.sec.ten_q_parser.__all__"
        )

    expected_digest = {
        "WeeklyInsiderSummary", "build_weekly_digest", "default_week_window",
    }
    for name in expected_digest:
        assert hasattr(insider_digest, name), (
            f"v2.sec.insider_digest missing {name}"
        )
        assert name in insider_digest.__all__, (
            f"{name} not in v2.sec.insider_digest.__all__"
        )

    # Sanity-check default_week_window for a known Friday — Mon-Fri
    # anchor is the whole behavioural contract.
    ws, we = insider_digest.default_week_window("2026-06-12")  # Fri
    assert ws == "2026-06-08" and we == "2026-06-12", (
        f"default_week_window('2026-06-12') returned ({ws!r}, {we!r}); "
        "expected ('2026-06-08', '2026-06-12') — Mon-Fri anchor regression"
    )

    log.info(
        "  ✓ v2.sec.ten_q_parser (3 public names) + v2.sec.insider_digest "
        "(3 public names) importable; Mon-Fri anchor verified for "
        "2026-06-12 Friday"
    )


# ---------------------------------------------------------------------------
# 2. ⑫b cron registered (Fri 19:15 ET)
# ---------------------------------------------------------------------------

def check_cron() -> None:
    """⑫b job present in scheduler with CronTrigger(fri 19:15)."""
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}

    assert "sec_insider_digest" in jobs, (
        f"sec_insider_digest job not registered. Got: {sorted(jobs.keys())}"
    )

    job = jobs["sec_insider_digest"]
    tf = {f.name: str(f) for f in job.trigger.fields}
    assert tf.get("hour") == "19", (
        f"⑫b hour expected 19, got {tf.get('hour')!r}"
    )
    assert tf.get("minute") == "15", (
        f"⑫b minute expected 15, got {tf.get('minute')!r}"
    )
    assert "fri" in tf.get("day_of_week", ""), (
        f"⑫b day_of_week expected to contain 'fri'; "
        f"got {tf.get('day_of_week')!r}"
    )

    log.info(
        "  ✓ ⑫b sec_insider_digest registered at Fri 19:15 ET — slots "
        "between ⑩ Portfolio Weekly (19:00) and ⑰ Macro Weekly (19:30)"
    )


# ---------------------------------------------------------------------------
# 3. Scheduler builds 20 jobs
# ---------------------------------------------------------------------------

def check_scheduler_20() -> None:
    """Phase 2.5 full landed at 19 jobs; Phase 3.5 adds ⑫b → 20."""
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    n = len(sched.get_jobs())
    assert n == 20, (
        f"scheduler builds {n} jobs, Phase 3.5 expected exactly 20 "
        "(Phase 2.5 full 19 + ⑫b sec_insider_digest)"
    )
    log.info("  ✓ scheduler builds exactly 20 jobs (19 + ⑫b)")


# ---------------------------------------------------------------------------
# 4. v2.earnings.pipeline._fetch_recent_ten_q exists with test seams
# ---------------------------------------------------------------------------

def check_pipeline_helper() -> None:
    """_fetch_recent_ten_q must expose fetcher / parser_fn / diff_fn
    kwargs — Stage 1 contract that keeps the cron-integration tests
    offline-runnable."""
    from v2.earnings import pipeline as p

    assert hasattr(p, "_fetch_recent_ten_q"), (
        "v2.earnings.pipeline._fetch_recent_ten_q missing — "
        "Stage 2 contract regression"
    )
    sig = inspect.signature(p._fetch_recent_ten_q)
    params = sig.parameters

    for seam in ("fetcher", "parser_fn", "diff_fn"):
        assert seam in params, (
            f"_fetch_recent_ten_q missing test seam kwarg {seam!r}; "
            f"got params: {list(params.keys())}"
        )
        assert params[seam].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{seam} should be keyword-only; got kind={params[seam].kind}"
        )
        assert params[seam].default is None, (
            f"{seam} default should be None (lazy import in helper); "
            f"got default={params[seam].default}"
        )

    log.info(
        "  ✓ _fetch_recent_ten_q(ticker, today_iso, *, fetcher=None, "
        "parser_fn=None, diff_fn=None) — 3 keyword-only test seams "
        "preserve sandbox-runnability"
    )


# ---------------------------------------------------------------------------
# 5. priority recognises going_concern + material_weakness
# ---------------------------------------------------------------------------

def check_priority() -> None:
    """v2.reporting.priority.compute_importance must apply +20 for
    has_going_concern and +15 for has_material_weakness on the
    earnings_summary kind, with the reason labels surfacing in the
    PriorityResult.reasons trail."""
    # Load priority module directly to bypass v2.reporting's heavy
    # __init__ (same trick the integration tests use).
    spec = importlib.util.spec_from_file_location(
        "v2.reporting.priority",
        _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    priority = importlib.util.module_from_spec(spec)
    sys.modules["v2.reporting.priority"] = priority
    spec.loader.exec_module(priority)

    # baseline — no flags
    baseline = priority.compute_importance(
        "earnings_summary", {"surprise_pct": 0.0},
    )

    # going_concern → +20
    gc = priority.compute_importance(
        "earnings_summary",
        {"surprise_pct": 0.0, "has_going_concern": True},
    )
    delta_gc = gc.score - baseline.score
    assert delta_gc == 20, (
        f"going_concern_in_10q should bump score by +20; "
        f"got delta={delta_gc} (baseline={baseline.score} → gc={gc.score})"
    )
    assert any("going_concern_in_10q" in r for r in gc.reasons), (
        f"reason 'going_concern_in_10q' missing from trail: {gc.reasons}"
    )

    # material_weakness → +15
    mw = priority.compute_importance(
        "earnings_summary",
        {"surprise_pct": 0.0, "has_material_weakness": True},
    )
    delta_mw = mw.score - baseline.score
    assert delta_mw == 15, (
        f"material_weakness_in_10q should bump score by +15; "
        f"got delta={delta_mw} (baseline={baseline.score} → mw={mw.score})"
    )
    assert any("material_weakness_in_10q" in r for r in mw.reasons), (
        f"reason 'material_weakness_in_10q' missing from trail: {mw.reasons}"
    )

    # And the sec_insider_digest kind exists for ⑫b
    digest_p = priority.compute_importance("sec_insider_digest", {})
    assert digest_p.tier == "P2" and digest_p.score == 55, (
        f"sec_insider_digest base should be P2/55; got "
        f"tier={digest_p.tier} score={digest_p.score}"
    )
    digest_p1 = priority.compute_importance(
        "sec_insider_digest", {"unusual_ticker_count": 3},
    )
    assert digest_p1.tier == "P1" and digest_p1.score == 65, (
        f"sec_insider_digest unusual≥3 should be P1/65; got "
        f"tier={digest_p1.tier} score={digest_p1.score}"
    )

    log.info(
        "  ✓ going_concern_in_10q (+20) + material_weakness_in_10q (+15) "
        "active on earnings_summary kind; sec_insider_digest base P2 / "
        "P1 unusual≥3 ladder wired"
    )


# ---------------------------------------------------------------------------
# 6. EarningsSummary has ten_q_delta field
# ---------------------------------------------------------------------------

def check_dataclass() -> None:
    """EarningsSummary dataclass carries ten_q_delta (Stage 2). Field
    is typed as ``object | None`` so v2.earnings doesn't import v2.sec
    at runtime — the formatter duck-types via getattr."""
    import dataclasses

    from v2.earnings.models import EarningsSummary

    fields = {f.name: f for f in dataclasses.fields(EarningsSummary)}
    assert "ten_q_delta" in fields, (
        f"EarningsSummary missing ten_q_delta field; got: {list(fields)}"
    )
    f = fields["ten_q_delta"]
    assert f.default is None, (
        f"ten_q_delta default should be None for backward-compat; "
        f"got default={f.default}"
    )

    # Sanity-construct EarningsSummary with no ten_q_delta — must
    # still succeed (Phase 1 cards should not break).
    es = EarningsSummary(
        ticker="X", report_period="2026-Q1", filing_date="2026-05-01",
        eps_surprise="BEAT",
    )
    assert es.ten_q_delta is None

    log.info(
        "  ✓ EarningsSummary.ten_q_delta: object | None = None — "
        "Phase 1 backward-compat preserved; duck-typed access in "
        "format_earnings_summary keeps v2.earnings → v2.sec decoupled"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("v2.sec.ten_q_parser + insider_digest importable",      check_modules),
        ("⑫b cron registered (Fri 19:15 ET)",                    check_cron),
        ("scheduler builds 20 jobs",                              check_scheduler_20),
        ("v2.earnings.pipeline._fetch_recent_ten_q test seams",  check_pipeline_helper),
        ("priority: going_concern +20 / material_weakness +15",  check_priority),
        ("EarningsSummary.ten_q_delta field",                     check_dataclass),
    ]

    log.info("Phase 3.5 — 10-Q Parser + Weekly Insider Digest")
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
    log.info("✅ Phase 3.5 全部就位 (6/6)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
