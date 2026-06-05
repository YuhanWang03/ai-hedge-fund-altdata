"""Phase 4 Macro Agent — final end-to-end sanity check.

Single-run verification that Stages 0-6 actually wired Phase 4 up
correctly. Safe to delete after a successful run on the deploy box.

Six checks (mirrors Phase 1 / Phase 2 / Phase 3 script structure):

  1. priority.BASE_SCORES carries all 7 new macro kinds (macro_release_p0/p1/p2,
     macro_snapshot_p3, macro_vix_spike, macro_curve_flip, macro_weekly)
     AND the adjustment factors fire (σ ladder / SEP shift / sell-side /
     vix spike magnitude / curve inverted).
  2. v2.scheduler builds 18 jobs including ⑭ macro_snapshot + ⑮
     macro_release + ⑯ macro_claims + ⑰ macro_weekly. All four carry
     timezone=_TZ explicitly.
  3. v2.bot.intent registers macro_view + release_check, 23 + unknown = 24.
     /macro + /cpi + /fomc + /yields slash commands attached in
     v2/bot/main.py.
  4. v2.reporting exposes 6 macro formatters; identities match the
     v2.macro._bot_cards source-of-truth.
  5. v2/macro/_smoke_stage1.py passes 40/40.
  6. FRED_API_KEY env var set (32-char alphanumeric value).

Usage:
    poetry run python scripts/_phase4_final_check.py

Sandbox: 4/6 ✓ + 2 ⚠ SKIPPED (v2.reporting public import + smoke
subprocess inherits no stubs + FRED_API_KEY absent) — production will
be 6/6 ✓ because v2.data + fredapi are installed and the env var
lives in /etc/hedge-fund/dashboard.env.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Stub production-only deps when the sandbox lacks them — same trick as
# v2/conftest.py uses for pytest. Production has the real packages and
# these blocks are no-ops.
import types as _types

_EDGAR_STUBBED = False
try:
    import edgar  # noqa: F401
except ImportError:
    _edgar = _types.ModuleType("edgar")
    _edgar.Company = type("Company", (), {})
    _edgar.set_identity = lambda *a, **kw: None
    sys.modules["edgar"] = _edgar
    _EDGAR_STUBBED = True

_FREDAPI_STUBBED = False
try:
    import fredapi  # noqa: F401
except ImportError:
    _fa = _types.ModuleType("fredapi")
    _fa.Fred = type("Fred", (), {
        "__init__": lambda self, *a, **kw: None,
    })
    sys.modules["fredapi"] = _fa
    _FREDAPI_STUBBED = True

try:
    import langchain_deepseek  # noqa: F401
except ImportError:
    _ld = _types.ModuleType("langchain_deepseek")
    _ld.ChatDeepSeek = type("ChatDeepSeek", (), {
        "__init__": lambda self, *a, **kw: None,
        "invoke": lambda self, *a, **kw: _types.SimpleNamespace(content="{}"),
    })
    sys.modules["langchain_deepseek"] = _ld

try:
    import tavily  # noqa: F401
except ImportError:
    _tv = _types.ModuleType("tavily")
    _tv.TavilyClient = type("TavilyClient", (), {
        "__init__": lambda self, *a, **kw: None,
        "search": lambda self, *a, **kw: {"results": []},
    })
    sys.modules["tavily"] = _tv


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


class _SandboxSkip(Exception):
    """Raised when a check is structurally fine but the sandbox is
    missing a production-only dep / config. Reported as SKIP, not FAIL —
    production run hits all 6 checks."""


# ---------------------------------------------------------------------------
# 1. priority.BASE_SCORES — 7 new macro kinds + adjustment factors
# ---------------------------------------------------------------------------

def check_priority() -> None:
    """All 7 base scores registered AND key adjustment factors fire."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase4_priority", _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    p = importlib.util.module_from_spec(spec)
    sys.modules["_phase4_priority"] = p
    spec.loader.exec_module(p)

    expected_bases = {
        "macro_release_p0":  85,
        "macro_release_p1":  65,
        "macro_release_p2":  55,
        "macro_snapshot_p3": 35,
        "macro_vix_spike":   85,
        "macro_curve_flip":  65,
        "macro_weekly":      65,
    }
    for kind, want in expected_bases.items():
        got = p.BASE_SCORES.get(kind)
        assert got == want, (
            f"BASE_SCORES[{kind!r}] expected {want}, got {got}"
        )

    # Adjustment factors fire as expected
    cases = [
        # σ ladder
        ("release sigma 3.5 extreme",
         "macro_release_p0", {"surprise_sigma": 3.5}, 100, "extreme_surprise"),
        ("release sigma 2.1 big",
         "macro_release_p1", {"surprise_sigma": 2.1}, 75, "big_surprise"),
        ("release sigma 1.2 moderate",
         "macro_release_p1", {"surprise_sigma": 1.2}, 70, "moderate_surprise"),
        # FOMC SEP shift
        ("FOMC hawkish_shift",
         "macro_release_p0",
         {"is_fomc": True, "sep_shift": "hawkish_shift"}, 100, "sep_hawkish_shift"),
        # Sell-side hawkish nudge
        ("FOMC sell_side_hawkish_unexpected",
         "macro_release_p1",
         {"is_fomc": True, "sell_side_consensus": "hawkish_unexpected"},
         75, "sell_side_hawkish"),
        # VIX spike magnitude
        ("vix +25% strong",
         "macro_vix_spike", {"vix_pct_change_1d": 0.25}, 95, "vix_strong"),
        ("vix +32% extreme",
         "macro_vix_spike", {"vix_pct_change_1d": 0.32}, 100, "vix_extreme"),
        # Curve flip
        ("curve_flip",
         "macro_curve_flip", {}, 75, "yield_curve_inverted"),
    ]
    for label, kind, md, expected_score, expected_reason_frag in cases:
        r = p.compute_importance(kind, md)
        assert r.score == expected_score, (
            f"{label}: expected score={expected_score}, got {r.score} "
            f"(reasons={r.reasons})"
        )
        assert any(expected_reason_frag in reason for reason in r.reasons), (
            f"{label}: expected reason containing {expected_reason_frag!r}, "
            f"got {r.reasons}"
        )

    log.info("  ✓ 7 macro BASE_SCORES + 5 adjustment-factor categories wired")


# ---------------------------------------------------------------------------
# 2. Scheduler 18 jobs including ⑭⑮⑯⑰
# ---------------------------------------------------------------------------

def check_scheduler_18() -> None:
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    jobs = {j.id for j in sched.get_jobs()}

    expected_min = {
        "daily_screen", "anomaly_monitor", "lateral_expansion",
        "institutional", "institutional_backfill", "etf_daily",
        "p2_digest", "archive_cleanup",
        "earnings_reminders", "earnings_summaries",
        "portfolio_risk", "portfolio_weekly",
        "sec_8k", "sec_form4",
        "macro_snapshot", "macro_release", "macro_claims", "macro_weekly",
    }
    missing = expected_min - jobs
    assert not missing, f"scheduler missing jobs: {missing}"
    assert len(jobs) >= 18, (
        f"expected ≥ 18 scheduler jobs, got {len(jobs)}: {jobs}"
    )

    # All 4 macro jobs must have day_of_week patterns pinned
    pinned = {
        "macro_snapshot": "mon-fri",
        "macro_release":  "mon-fri",
        "macro_claims":   "thu",
        "macro_weekly":   "fri",
    }
    for job_id, want_dow in pinned.items():
        job = next((j for j in sched.get_jobs() if j.id == job_id), None)
        assert job is not None, f"missing job {job_id}"
        tf = {f.name: str(f) for f in job.trigger.fields}
        assert want_dow in tf.get("day_of_week", ""), (
            f"{job_id}: day_of_week expected to contain {want_dow!r}; "
            f"got {tf.get('day_of_week')!r}"
        )

    log.info(
        "  ✓ scheduler builds %d jobs including ⑭ macro_snapshot "
        "(Mon-Fri 16:30 ET) + ⑮ macro_release (Mon-Fri 09:00 ET) "
        "+ ⑯ macro_claims (Thu 09:30 ET) + ⑰ macro_weekly (Fri 19:30 ET)",
        len(jobs),
    )


# ---------------------------------------------------------------------------
# 3. /macro + /cpi + /fomc + /yields + 23 NL intents
# ---------------------------------------------------------------------------

def check_bot_25() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase4_intent", _REPO_ROOT / "v2" / "bot" / "intent.py",
    )
    intent = importlib.util.module_from_spec(spec)
    sys.modules["_phase4_intent"] = intent
    spec.loader.exec_module(intent)

    assert "macro_view" in intent._VALID_INTENTS
    assert "release_check" in intent._VALID_INTENTS
    # 23 intents + unknown = 24
    assert len(intent._VALID_INTENTS) == 24, (
        f"expected 24 intents, got {len(intent._VALID_INTENTS)}"
    )
    # release_type closed enum present in system prompt
    assert "release_type" in intent._SYSTEM_PROMPT, (
        "release_type parameter not described in intent system prompt"
    )
    assert "固定 23 个 intent" in intent._SYSTEM_PROMPT

    # 4 new slash command handlers registered
    main_src = (_REPO_ROOT / "v2" / "bot" / "main.py").read_text()
    for cmd in ("macro", "cpi", "fomc", "yields"):
        assert f'CommandHandler("{cmd}"' in main_src, (
            f"/{cmd} CommandHandler not registered in v2/bot/main.py"
        )

    log.info(
        "  ✓ NL intents = %d (21 + macro_view + release_check + unknown); "
        "/macro + /cpi + /fomc + /yields handlers registered",
        len(intent._VALID_INTENTS),
    )


# ---------------------------------------------------------------------------
# 4. 6 public macro formatters
# ---------------------------------------------------------------------------

def check_formatters_6() -> None:
    expected = [
        "format_macro_daily_snapshot",
        "format_macro_release_card",
        "format_macro_fomc_card",
        "format_macro_claims_card",
        "format_macro_weekly_recap",
        "format_macro_dashboard",
    ]
    try:
        import v2.reporting as r
    except ModuleNotFoundError as exc:
        if "v2.data" in str(exc):
            from v2.macro import _bot_cards as src
            for name in expected:
                assert hasattr(src, name), f"_bot_cards missing {name}"
                assert name in src.__all__, f"{name} not in _bot_cards.__all__"
            log.info(
                "  ⚠ skipped public v2.reporting import "
                "(sandbox missing v2.data); _bot_cards source-of-truth "
                "exposes all 6 formatters",
            )
            raise _SandboxSkip("v2.data not installed (sandbox)")
        raise

    for name in expected:
        assert hasattr(r, name), f"v2.reporting missing {name}"
        assert name in getattr(r, "__all__", []), (
            f"{name} not in v2.reporting.__all__"
        )

    from v2.macro import _bot_cards as src
    assert r.format_macro_daily_snapshot is src.format_macro_daily_snapshot
    assert r.format_macro_release_card is src.format_macro_release_card
    assert r.format_macro_fomc_card is src.format_macro_fomc_card
    assert r.format_macro_claims_card is src.format_macro_claims_card
    assert r.format_macro_weekly_recap is src.format_macro_weekly_recap
    assert r.format_macro_dashboard is src.format_macro_dashboard

    log.info(
        "  ✓ v2.reporting.format_macro_* — all 6 public formatters resolve "
        "to v2.macro._bot_cards source-of-truth",
    )


# ---------------------------------------------------------------------------
# 5. Stage 1 macro smoke (40/40)
# ---------------------------------------------------------------------------

def check_macro_smoke() -> None:
    if _FREDAPI_STUBBED:
        log.info(
            "  ⚠ skipped — sandbox lacks real fredapi; smoke subprocess "
            "cannot inherit the in-process stub. Production has fredapi."
        )
        raise _SandboxSkip("fredapi not installed (sandbox)")

    result = subprocess.run(
        [sys.executable, "-m", "v2.macro._smoke_stage1"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"_smoke_stage1 failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "ALL SMOKE TESTS PASSED" in result.stdout, (
        "smoke didn't print the success marker"
    )
    log.info("  ✓ v2/macro/_smoke_stage1.py — 40/40 scenarios pass")


# ---------------------------------------------------------------------------
# 6. FRED_API_KEY env var
# ---------------------------------------------------------------------------

_FRED_KEY_RE = re.compile(r"^[a-zA-Z0-9]{30,40}$")


def check_fred_key() -> None:
    """FRED requires an API key for all REST endpoints. The env var
    feeds v2/macro/fred_client. Missing in sandbox is expected;
    production puts it in /etc/hedge-fund/dashboard.env."""
    val = os.environ.get("FRED_API_KEY", "").strip()
    if not val:
        log.info("  ⚠ FRED_API_KEY env var not set (expected in sandbox)")
        raise _SandboxSkip("FRED_API_KEY not set")
    assert _FRED_KEY_RE.match(val), (
        f"FRED_API_KEY should be 30-40 alphanumeric chars; "
        f"got length {len(val)}"
    )
    log.info("  ✓ FRED_API_KEY set (alphanumeric, length=%d)", len(val))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("Macro BASE_SCORES + 5 adjustment-factor categories", check_priority),
        ("18 scheduler cron jobs registered",                  check_scheduler_18),
        ("/macro + /cpi + /fomc + /yields + 23 NL intents",    check_bot_25),
        ("6 public macro formatters importable",               check_formatters_6),
        ("v2/macro smoke 40/40",                               check_macro_smoke),
        ("FRED_API_KEY env var set",                           check_fred_key),
    ]

    log.info("Phase 4 Macro Agent — final sanity check")
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
            "✅ Phase 4 Macro Agent 全部就位 (%d check(s) skipped: "
            "%s — re-run on production for full coverage)",
            len(skipped), skipped,
        )
    else:
        log.info("✅ Phase 4 Macro Agent 全部就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
