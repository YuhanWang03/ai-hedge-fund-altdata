"""Pure-logic tests for v2/reporting/priority.py — no I/O, no fixtures.

Loads priority.py via importlib so the test doesn't pull v2.reporting's
package __init__ (which transitively needs matplotlib / v2.data).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_PRIORITY_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "v2" / "reporting" / "priority.py"
)
_spec = importlib.util.spec_from_file_location("priority_under_test", _PRIORITY_PATH)
priority = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# Register in sys.modules BEFORE exec so @dataclass can resolve its module.
sys.modules["priority_under_test"] = priority
_spec.loader.exec_module(priority)

BASE_SCORES = priority.BASE_SCORES
compute_importance = priority.compute_importance
tier_chip_color = priority.tier_chip_color
tier_emoji_prefix = priority.tier_emoji_prefix


def test_anomaly_no_reasons_demoted_to_p3():
    r = compute_importance("anomaly_attribution", {"reasons_count": 0})
    # base 60 - 25 = 35 → P3
    assert r.score == 35
    assert r.tier == "P3"
    assert "-25_no_tavily_reasons" in r.reasons


def test_anomaly_with_reasons_stays_p1():
    r = compute_importance("anomaly_attribution", {"reasons_count": 3})
    # base 60 + 10 = 70 → P1
    assert r.score == 70
    assert r.tier == "P1"


def test_contrarian_move_promoted():
    r = compute_importance(
        "anomaly_attribution",
        {"reasons_count": 3, "flags": ["contrarian_move"],
         "price_change_pct": 0.06},
    )
    # base 60 + 10 (rich) + 15 (contrarian) + 10 (big_move) = 95 → P0
    assert r.score == 95
    assert r.tier == "P0"


def test_alert_fire_always_p0():
    r = compute_importance("alert_fire", {})
    assert r.score == 85
    assert r.tier == "P0"


def test_portfolio_loss_5_pct_promoted_to_p0():
    r = compute_importance("portfolio_risk", {"daily_pnl_pct": -0.05})
    # base 55 + 30 = 85 → P0
    assert r.score == 85
    assert r.tier == "P0"


def test_portfolio_moderate_loss_stays_p1():
    r = compute_importance("portfolio_risk", {"daily_pnl_pct": -0.03})
    # base 55 + 10 = 65 → P1
    assert r.score == 65
    assert r.tier == "P1"


def test_watchlist_adds_10():
    bare = compute_importance("anomaly_attribution", {"reasons_count": 2})
    with_watch = compute_importance(
        "anomaly_attribution",
        {"reasons_count": 2, "is_watchlist": True},
    )
    assert with_watch.score == bare.score + 10


def test_held_position_overrides_watchlist():
    """A held position should add +15, not +25 (watchlist+held). The held
    branch wins outright."""
    both = compute_importance(
        "anomaly_attribution",
        {"reasons_count": 2, "is_watchlist": True, "is_held_position": True},
    )
    held_only = compute_importance(
        "anomaly_attribution",
        {"reasons_count": 2, "is_held_position": True},
    )
    assert both.score == held_only.score
    assert "+15_held_position" in both.reasons
    assert "+10_watchlist" not in both.reasons


def test_earnings_surprise_above_10_pct_promoted():
    r = compute_importance("earnings_summary", {"surprise_pct": 0.15})
    # base 70 + 15 = 85 → P0
    assert r.tier == "P0"


def test_earnings_modest_surprise_not_promoted():
    r = compute_importance("earnings_summary", {"surprise_pct": 0.05})
    # base 70 stays
    assert r.tier == "P1"


def test_etf_significant_exit_touching_holding():
    r = compute_importance(
        "etf_significant",
        {"action": "exit", "ticker_held_by_user": True},
    )
    # base 75 + 10 + 15 = 100, capped, → P0
    assert r.tier == "P0"


def test_sec_critical_with_earnings_item():
    r = compute_importance("sec_critical", {"sec_item": "2.02"})
    # base 85 + 5 = 90 → P0
    assert r.tier == "P0"


def test_macro_critical_is_p0():
    r = compute_importance("macro_critical", {})
    assert r.tier == "P0"


def test_scheduler_status_is_p3():
    r = compute_importance("scheduler_status", {})
    assert r.tier == "P3"


def test_unknown_event_kind_defaults_to_p2():
    r = compute_importance("totally_new_kind", {})
    assert r.score == BASE_SCORES["default"]
    assert r.tier == "P2"


def test_score_clamped_to_0_100():
    # Construct a hypothetical that would overshoot
    r = compute_importance(
        "anomaly_attribution",
        {
            "reasons_count": 5,
            "flags": ["contrarian_move"],
            "price_change_pct": 0.20,
            "is_held_position": True,
        },
    )
    assert 0 <= r.score <= 100


def test_tier_emoji_prefix_table():
    assert tier_emoji_prefix("P0") == "🚨🚨🚨 "
    assert tier_emoji_prefix("P1") == ""
    assert tier_emoji_prefix("P2") == "📋 "
    assert tier_emoji_prefix("P3") == ""


def test_tier_chip_color_strings_present():
    for tier in ("P0", "P1", "P2", "P3"):
        klass = tier_chip_color(tier)
        assert "bg-" in klass
