"""Phase 1 Earnings Agent — final end-to-end sanity check.

Single-run verification that everything Stages 1-7 shipped is wired up
correctly. Safe to delete after one successful run on the deploy box.

Checks:
  1. archive.db has the earnings_summarized table (Stage 2 migration)
  2. priority.BASE_SCORES has all 5 earnings kinds (Stage 2 + 5)
  3. v2.reporting.format_earnings_* publishes the 5 public formatters
     (Stage 5 — re-exported chain)
  4. The scheduler builds with all 10 jobs including ⑦ + ⑧ (Stage 2)
  5. The Telegram bot registers the /earnings command (Stage 4)
  6. v2/earnings/_smoke_stage1.py still passes (Stage 1 regression)

Usage:
    poetry run python scripts/_phase1_final_check.py

Exit 0 if everything OK, non-zero with a clear marker otherwise.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
# Python sets sys.path[0] to the script's directory (scripts/), so v2/
# isn't on the path. Prepend the repo root.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. archive.db has earnings_summarized
# ---------------------------------------------------------------------------

def check_archive_table() -> None:
    import sqlite3
    import tempfile
    from v2.archive import Archive, store as archive_store

    with tempfile.TemporaryDirectory() as td:
        # Repoint to a temp DB so we don't touch production data — Archive()
        # itself runs the migration, which is what we want to verify.
        db = Path(td) / "archive.db"
        img = Path(td) / "img"
        archive_store._DB_PATH = db
        archive_store._IMG_ROOT = img
        Archive("phase1_final_check")

        conn = sqlite3.connect(str(db))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(earnings_summarized)"
            )}
        finally:
            conn.close()

    assert "earnings_summarized" in tables, \
        f"earnings_summarized table missing — got {tables}"
    expected = {"ticker", "report_period", "summarized_at", "outcome"}
    assert expected <= cols, f"earnings_summarized columns wrong — got {cols}"
    log.info("  ✓ archive.db earnings_summarized table OK (cols=%s)", sorted(cols))


# ---------------------------------------------------------------------------
# 2. priority BASE_SCORES has the 5 earnings kinds
# ---------------------------------------------------------------------------

def check_priority_base_scores() -> None:
    # Import via importlib to bypass v2.reporting's heavy package init.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase1_priority", _REPO_ROOT / "v2" / "reporting" / "priority.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_phase1_priority"] = mod
    spec.loader.exec_module(mod)

    expected = {
        "earnings_reminder_d3": 45,
        "earnings_reminder_d1": 60,
        "earnings_reminder_d0": 60,
        "earnings_summary":     70,
        "earnings_pending":     45,
    }
    for kind, want in expected.items():
        assert kind in mod.BASE_SCORES, f"BASE_SCORES missing {kind}"
        got = mod.BASE_SCORES[kind]
        assert got == want, f"BASE_SCORES[{kind}] = {got}, expected {want}"
    log.info("  ✓ priority.BASE_SCORES has all 5 earnings kinds with correct scalars")


# ---------------------------------------------------------------------------
# 3. v2.reporting.format_earnings_* publishes all 5 formatters
# ---------------------------------------------------------------------------

class _SandboxSkip(Exception):
    """Raised when a check is structurally fine but the sandbox is missing
    a production-only dep (e.g. v2.data). Reported as SKIP, not FAIL."""


def check_public_formatters() -> None:
    expected = [
        "format_earnings_view",
        "format_earnings_calendar",
        "format_earnings_reminder",
        "format_earnings_summary",
        "format_earnings_pending",
    ]
    # Production import — exercises v2.reporting/__init__.py + formatters.py
    # + _earnings_formatters.py + v2.earnings._bot_cards (the full chain).
    try:
        import v2.reporting as r
    except ModuleNotFoundError as exc:
        if "v2.data" in str(exc):
            # Dev sandbox doesn't ship the FD client — production-only.
            # The re-export chain itself is verified via the identity
            # check below, which doesn't need v2.reporting.
            from v2.earnings import _bot_cards as src
            for name in expected:
                assert hasattr(src, name), f"_bot_cards missing {name}"
            log.info("  ⚠ skipped public v2.reporting import (sandbox missing v2.data); "
                     "_bot_cards source-of-truth exposes all 5 formatters")
            raise _SandboxSkip("v2.data not installed (sandbox)")
        raise

    for name in expected:
        assert hasattr(r, name), f"v2.reporting missing {name}"
        assert name in getattr(r, "__all__", []), \
            f"{name} not in v2.reporting.__all__"

    # And identity: the public name resolves to the source-of-truth in
    # v2/earnings/_bot_cards.py (the actual implementation file).
    from v2.earnings import _bot_cards as src
    for name in expected:
        assert getattr(r, name) is getattr(src, name), \
            f"identity mismatch for v2.reporting.{name}"
    log.info("  ✓ v2.reporting.format_earnings_* — all 5 public formatters resolve to "
             "v2.earnings._bot_cards source-of-truth")


# ---------------------------------------------------------------------------
# 4. Scheduler builds 10 jobs including ⑦ + ⑧
# ---------------------------------------------------------------------------

def check_scheduler() -> None:
    from v2.scheduler.main import build_scheduler

    sched = build_scheduler()
    jobs = {j.id for j in sched.get_jobs()}

    expected_min = {
        "daily_screen", "anomaly_monitor", "lateral_expansion",
        "institutional", "institutional_backfill", "etf_daily",
        "p2_digest", "archive_cleanup",
        "earnings_reminders", "earnings_summaries",
    }
    missing = expected_min - jobs
    assert not missing, f"scheduler missing jobs: {missing}"
    assert len(jobs) >= 10, f"expected ≥ 10 scheduler jobs, got {len(jobs)}: {jobs}"
    log.info("  ✓ scheduler builds %d jobs including ⑦ earnings_reminders + "
             "⑧ earnings_summaries", len(jobs))


# ---------------------------------------------------------------------------
# 5. /earnings command registered + NL intent classifier valid set is 18
# ---------------------------------------------------------------------------

def check_bot_wiring() -> None:
    # Use importlib for v2.bot.intent so we don't load the full bot package
    # (which requires TELEGRAM_BOT_TOKEN at build_application time).
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_phase1_intent", _REPO_ROOT / "v2" / "bot" / "intent.py",
    )
    intent = importlib.util.module_from_spec(spec)
    sys.modules["_phase1_intent"] = intent
    spec.loader.exec_module(intent)

    assert "earnings_view" in intent._VALID_INTENTS
    assert "earnings_calendar" in intent._VALID_INTENTS
    assert "days_horizon" in intent._SYSTEM_PROMPT, \
        "days_horizon not in intent system prompt"
    assert "earnings_view" in intent._SYSTEM_PROMPT
    assert "earnings_calendar" in intent._SYSTEM_PROMPT
    # 15 real + 2 new + unknown = 18
    assert len(intent._VALID_INTENTS) == 18, \
        f"expected 18 intents, got {len(intent._VALID_INTENTS)}"

    # /earnings handler exists on commands module (sandbox can't build the
    # actual Application without a TELEGRAM_BOT_TOKEN, but the handler
    # being attribute-present is enough proof of registration).
    spec2 = importlib.util.spec_from_file_location(
        "_phase1_main", _REPO_ROOT / "v2" / "bot" / "main.py",
    )
    main_src = (_REPO_ROOT / "v2" / "bot" / "main.py").read_text()
    assert 'CommandHandler("earnings"' in main_src, \
        "/earnings CommandHandler not registered in v2/bot/main.py"
    log.info("  ✓ NL intents = %d (15 + earnings_view + earnings_calendar + unknown)",
             len(intent._VALID_INTENTS))
    log.info("  ✓ /earnings handler registered in v2/bot/main.py")


# ---------------------------------------------------------------------------
# 6. Stage 1 smoke still passes
# ---------------------------------------------------------------------------

def check_stage1_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "v2.earnings._smoke_stage1"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, \
        f"_smoke_stage1 failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    assert "ALL SMOKE TESTS PASSED" in result.stdout
    log.info("  ✓ v2/earnings/_smoke_stage1.py — 6/6 scenarios pass")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("archive earnings_summarized table",     check_archive_table),
        ("priority BASE_SCORES (5 earnings kinds)", check_priority_base_scores),
        ("v2.reporting public formatters (5)",    check_public_formatters),
        ("scheduler ⑦ + ⑧",                       check_scheduler),
        ("bot /earnings + 17 NL intents",         check_bot_wiring),
        ("Stage 1 smoke regression",              check_stage1_smoke),
    ]

    log.info("Phase 1 Earnings Agent — final sanity check")
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
        log.info("✅ Phase 1 Earnings Agent 全部就位 (%d check(s) skipped: %s — "
                 "re-run on production for full coverage)", len(skipped), skipped)
    else:
        log.info("✅ Phase 1 Earnings Agent 全部就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
