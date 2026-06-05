"""Phase 4.5-mini final verification — run once after deploy, then delete.

Single-run sanity check that Stages 1-3 wired the daily-prices
migration up correctly. Safe to delete after a successful production
run (or after observing ⑭ + ② cron output for a week — Phase 4.5-mini
is small enough that one push cycle is plenty validation).

Five checks (mirrors the Phase 1/2/3/4 final-check structure):

  1. ``v2.data.price_source`` module importable + public surface
     (4 names: PriceSource / FDPriceSource / YFinancePriceSource /
     default_price_source).
  2. ``default_price_source()`` with no env override returns a
     :class:`YFinancePriceSource` instance.
  3. ``v2/data_safety.py`` file is absent from disk (Stage 3
     deletion confirmed).
  4. Zero grep hits for ``fd_safe_today`` / ``v2.data_safety`` in
     production code outside the historical archaeology breadcrumb
     (``v2/data/price_source.py`` docstring).
  5. All 3 cron scripts + 1 utility explicitly inject the price
     source via ``default_price_source()`` so ops can grep the
     entry points and see the choice (Stage 2 spec requirement).

Usage:
    poetry run python scripts/_phase4_5_mini_final_check.py

Expected: 5/5 ✓ on both sandbox and production. The smoke tests on
this stage are runtime-mockable so v2.data being production-only
doesn't gate the import check.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import types
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Sandbox stubs for v2.data + v2.data.models — production-only modules
# that the sandbox lacks. Mirrors the test_price_source.py harness so
# this script is callable from either environment without extra setup.
if "v2.data" not in sys.modules or not hasattr(
    sys.modules.get("v2.data"), "CachedFDClient",
):
    _v2_data = types.ModuleType("v2.data")
    _v2_data.__path__ = []
    _v2_data.CachedFDClient = type("CachedFDClient", (), {})
    _v2_data.FDClient = type("FDClient", (), {})
    sys.modules["v2.data"] = _v2_data

if "v2.data.models" not in sys.modules:
    from dataclasses import dataclass
    from datetime import date as _date

    @dataclass
    class _Price:
        date: _date
        open: float
        high: float
        low: float
        close: float
        volume: int

    _v2_data_models = types.ModuleType("v2.data.models")
    _v2_data_models.Price = _Price
    sys.modules["v2.data.models"] = _v2_data_models


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. v2.data.price_source importable + public surface
# ---------------------------------------------------------------------------

def check_module() -> None:
    """Module loads cleanly + exports the 4 documented names."""
    # Load via importlib to bypass any cached v2.data side-effects.
    spec = importlib.util.spec_from_file_location(
        "_phase4_5_mini_ps",
        _REPO_ROOT / "v2" / "data" / "price_source.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_phase4_5_mini_ps"] = mod
    spec.loader.exec_module(mod)

    expected = {
        "PriceSource", "FDPriceSource", "YFinancePriceSource",
        "default_price_source",
    }
    for name in expected:
        assert hasattr(mod, name), f"v2.data.price_source missing {name}"
        assert name in mod.__all__, f"{name} not in __all__"

    log.info(
        "  ✓ v2/data/price_source.py — 4 public names exposed "
        "(PriceSource / FDPriceSource / YFinancePriceSource / "
        "default_price_source)",
    )


# ---------------------------------------------------------------------------
# 2. default_price_source() returns YFinancePriceSource by default
# ---------------------------------------------------------------------------

def check_factory() -> None:
    """No env override → YFinancePriceSource. Production default is
    real-time EOD via yfinance; FD is only the escape hatch."""
    # Make sure we don't trip over a leftover env var
    prev = os.environ.pop("V2_PRICE_SOURCE", None)
    try:
        # Use the cached module from check_module() to avoid double-load.
        mod = sys.modules["_phase4_5_mini_ps"]
        src = mod.default_price_source()
        assert isinstance(src, mod.YFinancePriceSource), (
            f"default_price_source() returned {type(src).__name__}, "
            f"expected YFinancePriceSource"
        )
    finally:
        if prev is not None:
            os.environ["V2_PRICE_SOURCE"] = prev

    log.info(
        "  ✓ default_price_source() — yfinance default; "
        "V2_PRICE_SOURCE=fd flips to FDPriceSource",
    )


# ---------------------------------------------------------------------------
# 3. v2/data_safety.py absent (Stage 3 deletion)
# ---------------------------------------------------------------------------

def check_data_safety_removed() -> None:
    """The Stage 3 deletion is durable — file gone from disk + git."""
    path = _REPO_ROOT / "v2" / "data_safety.py"
    assert not path.exists(), (
        f"{path} still on disk — Stage 3 deletion was incomplete"
    )
    log.info("  ✓ v2/data_safety.py absent (Stage 3 deletion confirmed)")


# ---------------------------------------------------------------------------
# 4. Zero grep hits for fd_safe_today outside the archaeology breadcrumb
# ---------------------------------------------------------------------------

_GREP_PATTERN = re.compile(r"fd_safe_today|v2\.data_safety|data_safety")

# Roots to scan + extensions (.py).
_SCAN_ROOTS = ("v2", "scripts", "dashboard/backend")

# Files where a historical reference is intentional and not a live link.
_ARCHAEOLOGY_ALLOWLIST: set[Path] = {
    # v2/data/price_source.py docstring narrates why the module exists.
    Path("v2/data/price_source.py"),
    # This script itself contains the regex pattern + module names.
    Path("scripts/_phase4_5_mini_final_check.py"),
}


def check_grep() -> None:
    """Walk the in-scope tree; any hit outside the breadcrumb fails."""
    offenders: list[tuple[Path, int, str]] = []
    for root_name in _SCAN_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            rel = p.relative_to(_REPO_ROOT)
            if rel in _ARCHAEOLOGY_ALLOWLIST:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _GREP_PATTERN.search(line):
                    offenders.append((rel, lineno, line.strip()[:120]))

    assert not offenders, (
        "fd_safe_today / v2.data_safety references still present:\n  "
        + "\n  ".join(f"{p}:{n}: {snippet}" for p, n, snippet in offenders)
    )
    log.info(
        "  ✓ zero fd_safe_today / v2.data_safety references in production "
        "code (archaeology breadcrumb in v2/data/price_source.py docstring "
        "intentionally preserved)",
    )


# ---------------------------------------------------------------------------
# 5. 3 cron scripts + 1 utility wire price_source explicitly
# ---------------------------------------------------------------------------

def check_injection() -> None:
    """Each in-scope entry point imports default_price_source and uses
    the result. Catches a regression where someone reverts the explicit
    injection to rely on the kwarg default.

    Two acceptable shapes:
    - Cron scripts pass ``price_source=...`` into ``run_screening`` /
      ``run_monitoring`` / ``run_lateral_expansion`` (the wrapping
      functions take it as a kwarg).
    - The utility (``dump_universe_metrics.py``) calls
      ``price_source.get_prices(...)`` directly — there's no wrapping
      function to inject into.
    """
    # (path, ok_marker) — at least one of the markers must be present.
    targets: list[tuple[str, tuple[str, ...]]] = [
        ("scripts/anomaly_to_telegram.py",        ("price_source=",)),
        ("scripts/daily_screen_to_telegram.py",   ("price_source=",)),
        ("scripts/lateral_to_telegram.py",        ("price_source=",)),
        ("scripts/dump_universe_metrics.py",      (
            "price_source.get_prices", "price_source=",
        )),
    ]
    failures: list[str] = []
    for rel, ok_markers in targets:
        path = _REPO_ROOT / rel
        if not path.exists():
            failures.append(f"{rel}: missing")
            continue
        text = path.read_text(encoding="utf-8")
        if "from v2.data.price_source import default_price_source" not in text:
            failures.append(f"{rel}: missing default_price_source import")
            continue
        if "default_price_source()" not in text:
            failures.append(f"{rel}: imports default_price_source but never calls it")
            continue
        if not any(marker in text for marker in ok_markers):
            failures.append(
                f"{rel}: doesn't pass / use price_source ({ok_markers!r})"
            )

    assert not failures, (
        "Explicit price_source injection broken:\n  "
        + "\n  ".join(failures)
    )
    log.info(
        "  ✓ 3 cron scripts + 1 utility all explicitly construct "
        "default_price_source() at the entry point",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("v2.data.price_source importable + 4 public names",      check_module),
        ("default_price_source() returns YFinancePriceSource",    check_factory),
        ("v2/data_safety.py absent (Stage 3 deletion)",           check_data_safety_removed),
        ("0 hits for fd_safe_today / v2.data_safety in src",      check_grep),
        ("3 cron + 1 utility explicit price_source injection",    check_injection),
    ]

    log.info("Phase 4.5-mini — Daily Prices Migration final check")
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
    log.info("✅ Phase 4.5-mini Daily Prices Migration 全部就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
