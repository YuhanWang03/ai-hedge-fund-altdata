"""Observability SDK for the dashboard.

Production Telegram bot, scheduler, and streamer never import or call anything
from this package — it is dormant unless the dashboard backend explicitly
calls install_all() at startup. That keeps the existing services' behavior
bit-for-bit identical.

Public surface:
    Trace, Event, TRACE_CTX, current_trace, emit
    install_all()
    estimate_cost(...)
"""

from v2.observability.trace import (
    Event,
    TRACE_CTX,
    Trace,
    current_trace,
    emit,
)
from v2.observability.hooks import install_all, installed_hooks
from v2.observability.pricing import estimate_cost

__all__ = [
    "Event",
    "TRACE_CTX",
    "Trace",
    "current_trace",
    "emit",
    "install_all",
    "installed_hooks",
    "estimate_cost",
]
