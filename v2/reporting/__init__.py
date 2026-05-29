"""Reporting layer — convert results into messages and push them out."""

from v2.reporting.error_handler import notify_on_error
from v2.reporting.formatters import (
    format_alert_fired,
    format_alert_list,
    format_anomaly_alert,
    format_backtest_summary,
    format_etf_snapshot,
    format_holders,
    format_institutional_messages,
    format_intraday_anomaly,
    format_lateral_result,
    format_pnl,
    format_portfolio,
    format_portfolio_snapshot,
    format_screening_result,
    render_equity_curve,
    render_price_sparkline,
)
from v2.reporting.notifier import ConsoleNotifier, Notifier, TelegramNotifier

__all__ = [
    "ConsoleNotifier",
    "Notifier",
    "TelegramNotifier",
    "format_alert_fired",
    "format_alert_list",
    "format_anomaly_alert",
    "format_backtest_summary",
    "format_etf_snapshot",
    "format_holders",
    "format_institutional_messages",
    "format_intraday_anomaly",
    "format_lateral_result",
    "format_pnl",
    "format_portfolio",
    "format_portfolio_snapshot",
    "format_screening_result",
    "notify_on_error",
    "render_equity_curve",
    "render_price_sparkline",
]
