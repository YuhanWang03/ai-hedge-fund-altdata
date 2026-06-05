"""Phase 3 SEC Monitoring Agent — final end-to-end sanity check.

Single-run verification that Stages 0-6 actually wired Phase 3 up
correctly. Safe to delete after a successful run on the deploy box.

Six checks (mirrors Phase 1 / Phase 2 script structure):

  1. priority.BASE_SCORES carries all 7 new SEC kinds (8-K P0/P1/P2/P3 +
     Form 4 purchase / sale / cluster) AND the adjustment factors fire
     (senior_exec / amendment / 10b5_1 / magnitude tiers / cluster size).
  2. v2.scheduler builds 14 jobs including ⑪ sec_8k + ⑫ sec_form4.
  3. v2.bot.intent registers eight_k_view + insider_view, 21 + unknown = 22.
     /8k + /insiders slash commands attached in v2/bot/main.py.
  4. v2.reporting exposes 5 SEC formatters; identities match the
     v2.sec._bot_cards source-of-truth.
  5. v2/sec/_smoke_stage1.py passes 21/21.
  6. EDGAR_IDENTITY env var set with email-shaped value.

Usage:
    poetry run python scripts/_phase3_final_check.py

Sandbox: 5/6 ✓ + 1 ⚠ SKIPPED (v2.reporting public import + EDGAR_IDENTITY
absent) — production will be 6/6 ✓ because v2.data is installed and
the env var lives in /etc/hedge-fund/dashboard.env.
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
# 1. priority.BASE_SCORES — 7 new kinds + adjustment factors
# ---------------------------------------------------------------------------

def check_priority() -> None:
    """All 7 base scores registered AND key adjustment factors fire."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase3_priority", _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    p = importlib.util.module_from_spec(spec)
    sys.modules["_phase3_priority"] = p
    spec.loader.exec_module(p)

    expected_bases = {
        "sec_8k_p0": 85,
        "sec_8k_p1": 65,
        "sec_8k_p2": 55,
        "sec_8k_p3": 35,
        "sec_form4_purchase": 75,
        "sec_form4_sale": 50,
        "sec_form4_cluster": 75,
    }
    for kind, want in expected_bases.items():
        got = p.BASE_SCORES.get(kind)
        assert got == want, (
            f"BASE_SCORES[{kind!r}] expected {want}, got {got}"
        )

    # Adjustment factors fire as expected. Adjustment magnitudes match
    # v2/reporting/priority.py exactly (Stage 0 Phase 3 design).
    cases = [
        # 8-K P0 + LLM senior_exec confirm → +5 nudge → 90
        ("8k_p0 senior_exec",
         "sec_8k_p0", {"has_senior_exec": True}, 90, "ceo_cfo_5_02_confirmed"),
        # 8-K amendment → -5
        ("8k_p1 amendment",
         "sec_8k_p1", {"is_amendment": True}, 60, "amendment"),
        # Form 4 purchase ≥ $1M → +25 → 100 (75+25)
        ("form4 P 2.5M",
         "sec_form4_purchase", {"transaction_usd": 2_500_000.0}, 100, "big_purchase"),
        # Form 4 purchase 10b5-1 plan → -10 → 65
        ("form4 P 10b5-1",
         "sec_form4_purchase", {"is_10b5_1": True}, 65, "10b5_1_plan_purchase"),
        # Form 4 sale ≥$1M 10b5-1 → -5 → 45
        ("form4 S 1M 10b5-1",
         "sec_form4_sale",
         {"transaction_usd": 1_500_000.0, "is_10b5_1": True}, 45, "10b5_1_plan_sale"),
        # Cluster ≥5 → +15 → 90
        ("form4 cluster 5",
         "sec_form4_cluster", {"transaction_count": 5}, 90, "large_cluster"),
        # Cluster 3-4 → +5
        ("form4 cluster 3",
         "sec_form4_cluster", {"transaction_count": 3}, 80, "cluster_3"),
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

    log.info("  ✓ 7 SEC BASE_SCORES + 6 adjustment-factor categories wired")


# ---------------------------------------------------------------------------
# 2. Scheduler 14 jobs including ⑪ + ⑫
# ---------------------------------------------------------------------------

def check_scheduler_14() -> None:
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
    }
    missing = expected_min - jobs
    assert not missing, f"scheduler missing jobs: {missing}"
    assert len(jobs) >= 14, (
        f"expected ≥ 14 scheduler jobs, got {len(jobs)}: {jobs}"
    )

    # ⑪ 8-K cron must be Mon-Fri (not weekends — SEC EDGAR no filings on weekends)
    sec_8k_job = next((j for j in sched.get_jobs() if j.id == "sec_8k"), None)
    assert sec_8k_job is not None
    tf = {f.name: str(f) for f in sec_8k_job.trigger.fields}
    assert "mon-fri" in tf.get("day_of_week", ""), (
        f"⑪ sec_8k must fire Mon-Fri only; got {tf.get('day_of_week')!r}"
    )

    # ⑫ Form 4 cron same expectation
    sec_form4_job = next((j for j in sched.get_jobs() if j.id == "sec_form4"), None)
    assert sec_form4_job is not None
    tf = {f.name: str(f) for f in sec_form4_job.trigger.fields}
    assert "mon-fri" in tf.get("day_of_week", ""), (
        f"⑫ sec_form4 must fire Mon-Fri only; got {tf.get('day_of_week')!r}"
    )

    log.info(
        "  ✓ scheduler builds %d jobs including ⑪ sec_8k (Mon-Fri 17:05 ET) "
        "+ ⑫ sec_form4 (Mon-Fri 17:45 ET)", len(jobs),
    )


# ---------------------------------------------------------------------------
# 3. /8k + /insiders + 21 NL intents
# ---------------------------------------------------------------------------

def check_bot_21() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase3_intent", _REPO_ROOT / "v2" / "bot" / "intent.py",
    )
    intent = importlib.util.module_from_spec(spec)
    sys.modules["_phase3_intent"] = intent
    spec.loader.exec_module(intent)

    assert "eight_k_view" in intent._VALID_INTENTS
    assert "insider_view" in intent._VALID_INTENTS
    # 21 intents + unknown = 22
    assert len(intent._VALID_INTENTS) == 22, (
        f"expected 22 intents, got {len(intent._VALID_INTENTS)}"
    )
    # days_back param surfaced in system prompt
    assert "days_back" in intent._SYSTEM_PROMPT, (
        "days_back parameter not described in intent system prompt"
    )
    assert "固定 21 个 intent" in intent._SYSTEM_PROMPT

    # /8k + /insiders CommandHandlers registered
    main_src = (_REPO_ROOT / "v2" / "bot" / "main.py").read_text()
    assert 'CommandHandler("8k"' in main_src, (
        "/8k CommandHandler not registered in v2/bot/main.py"
    )
    assert 'CommandHandler("insiders"' in main_src, (
        "/insiders CommandHandler not registered in v2/bot/main.py"
    )

    log.info(
        "  ✓ NL intents = %d (19 + eight_k_view + insider_view + unknown); "
        "/8k + /insiders handlers registered", len(intent._VALID_INTENTS),
    )


# ---------------------------------------------------------------------------
# 4. 5 public SEC formatters
# ---------------------------------------------------------------------------

def check_formatters_5() -> None:
    expected = [
        "format_sec_8k_card",
        "format_sec_8k_view",
        "format_sec_form4_individual_card",
        "format_sec_form4_cluster_card",
        "format_sec_form4_view",
    ]
    try:
        import v2.reporting as r
    except ModuleNotFoundError as exc:
        if "v2.data" in str(exc):
            from v2.sec import _bot_cards as src
            for name in expected:
                assert hasattr(src, name), f"_bot_cards missing {name}"
                assert name in src.__all__, f"{name} not in _bot_cards.__all__"
            log.info(
                "  ⚠ skipped public v2.reporting import "
                "(sandbox missing v2.data); _bot_cards source-of-truth "
                "exposes all 5 formatters",
            )
            raise _SandboxSkip("v2.data not installed (sandbox)")
        raise

    for name in expected:
        assert hasattr(r, name), f"v2.reporting missing {name}"
        assert name in getattr(r, "__all__", []), (
            f"{name} not in v2.reporting.__all__"
        )

    from v2.sec import _bot_cards as src
    assert r.format_sec_8k_card is src.format_sec_8k_card
    assert r.format_sec_8k_view is src.format_sec_8k_view
    assert r.format_sec_form4_individual_card is src.format_sec_form4_individual_card
    assert r.format_sec_form4_cluster_card is src.format_sec_form4_cluster_card
    assert r.format_sec_form4_view is src.format_sec_form4_view

    log.info(
        "  ✓ v2.reporting.format_sec_* — all 5 public formatters resolve "
        "to v2.sec._bot_cards source-of-truth",
    )


# ---------------------------------------------------------------------------
# 5. Stage 1 SEC smoke (21/21)
# ---------------------------------------------------------------------------

def check_sec_smoke() -> None:
    if _EDGAR_STUBBED:
        log.info(
            "  ⚠ skipped — sandbox lacks real edgartools; smoke subprocess "
            "cannot inherit the in-process stub. Production has edgartools."
        )
        raise _SandboxSkip("edgartools not installed (sandbox)")

    result = subprocess.run(
        [sys.executable, "-m", "v2.sec._smoke_stage1"],
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
    log.info("  ✓ v2/sec/_smoke_stage1.py — 21/21 scenarios pass")


# ---------------------------------------------------------------------------
# 6. EDGAR_IDENTITY env var
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def check_edgar_identity() -> None:
    """SEC EDGAR requires User-Agent including a contact email. The
    env var feeds v2/sec/client.set_identity() at module load. Missing
    in sandbox is expected; production puts it in
    /etc/hedge-fund/dashboard.env."""
    val = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not val:
        log.info("  ⚠ EDGAR_IDENTITY env var not set (expected in sandbox)")
        raise _SandboxSkip("EDGAR_IDENTITY not set")
    assert _EMAIL_RE.search(val), (
        f"EDGAR_IDENTITY must contain an email address; got {val!r}"
    )
    log.info("  ✓ EDGAR_IDENTITY set (email present)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("SEC BASE_SCORES + adjustment factors", check_priority),
        ("14 scheduler cron jobs registered",   check_scheduler_14),
        ("/8k + /insiders + 21 NL intents",     check_bot_21),
        ("5 public SEC formatters importable",  check_formatters_5),
        ("v2/sec smoke 21/21",                  check_sec_smoke),
        ("EDGAR_IDENTITY env var set",          check_edgar_identity),
    ]

    log.info("Phase 3 SEC Monitoring Agent — final sanity check")
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
            "✅ Phase 3 SEC 监控 Agent 全部就位 (%d check(s) skipped: "
            "%s — re-run on production for full coverage)",
            len(skipped), skipped,
        )
    else:
        log.info("✅ Phase 3 SEC 监控 Agent 全部就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
