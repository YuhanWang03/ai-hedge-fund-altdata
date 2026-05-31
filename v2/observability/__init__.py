"""Observability SDK for the dashboard.

Production Telegram bot, scheduler, and streamer never import or call anything
from this package — it is dormant unless the dashboard backend explicitly
calls install_all() at startup. That keeps the existing services' behavior
bit-for-bit identical.

Public surface:
    Trace, Event, TRACE_CTX, current_trace, emit
    capture_trace()   (context manager — used by scheduler scripts)
    install_all()
    estimate_cost(...)
"""

import uuid
from contextlib import contextmanager

from v2.observability.trace import (
    Event,
    TRACE_CTX,
    Trace,
    current_trace,
    emit,
)
from v2.observability.hooks import install_all, installed_hooks
from v2.observability.pricing import estimate_cost


@contextmanager
def capture_trace(session_id: str | None = None):
    """Capture all v2/ trace events emitted inside the block.

    Usage in scheduler scripts:

        with capture_trace() as trace:
            attribute(anomaly, ...)
        notifier.send_text(formatted, trace=trace, title="…")

    The yielded Trace exposes `trace.events` — a list[dict] of every emit()
    that happened inside the block (in arrival order). When the block exits,
    TRACE_CTX is reset so nested or subsequent calls don't leak.

    Outside a dashboard context, install_all() is never called — so the
    monkey-patched hooks aren't installed and emit() is a no-op. The
    scheduler is expected to call install_all() once at startup if it wants
    the events captured.
    """
    sid = session_id or f"capture_{uuid.uuid4().hex[:12]}"
    events: list = []
    trace = Trace(session_id=sid, sink=lambda ev: events.append(ev))
    # `Trace` is a dataclass — adding an attribute at runtime is fine.
    trace.events = events  # type: ignore[attr-defined]
    token = TRACE_CTX.set(trace)
    try:
        yield trace
    finally:
        TRACE_CTX.reset(token)


__all__ = [
    "Event",
    "TRACE_CTX",
    "Trace",
    "current_trace",
    "emit",
    "capture_trace",
    "install_all",
    "installed_hooks",
    "estimate_cost",
]
