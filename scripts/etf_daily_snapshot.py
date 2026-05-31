"""Daily ARK ETF holdings snapshot — runs after US market close.

For each ARK fund, fetches the latest CSV and saves to data/etf.db.
Builds a cumulative time series that the bot's /etf command uses to
compute 24h rebalances.

Telegram-quiet by design: pure data ingestion. The dashboard auto-push
feed picks it up via a direct archive write (no Telegram message),
so the trace still appears in the dashboard.

Triggered by the scheduler Mon-Fri 17:00 ET — ARK posts daily files
around 16:00 ET, giving us an hour of buffer.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from v2.archive import Archive
from v2.etf import SUPPORTED_FUNDS, fetch_holdings, save_snapshot
from v2.observability import capture_trace_with_framing, install_all
from v2.reporting import notify_on_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@notify_on_error("etf-daily")
def main() -> int:
    load_dotenv()
    install_all()

    total_funds = 0
    total_rows = 0
    fetched: list[str] = []
    last_snap_date = ""

    # Capture the full 4-fund pipeline as one trace so the dashboard feed
    # can replay every fetch + save snapshot. capture_trace_with_framing
    # auto-emits session_start / intent_classified / module_enter on entry
    # and module_exit / session_end on exit, matching the dashboard
    # executor's frame so PipelineBar lights up.
    with capture_trace_with_framing(
        agent="etf", intent="etf_view",
        text="(自动推送) ARK 每日持仓",
        responder_name="_r_etf_snapshot",
    ) as trace:
        for symbol in SUPPORTED_FUNDS:
            logger.info("Fetching %s ...", symbol)
            try:
                holdings, snap_date = fetch_holdings(symbol)
            except Exception as exc:
                logger.warning("  %s fetch failed: %s", symbol, exc)
                continue

            if not holdings:
                logger.warning("  %s returned no holdings", symbol)
                continue

            try:
                n = save_snapshot(symbol, snap_date, holdings)
                logger.info("  %s · %s · %d rows saved", symbol, snap_date, n)
                total_funds += 1
                total_rows += n
                fetched.append(symbol)
                last_snap_date = snap_date
            except Exception as exc:
                logger.warning("  %s save failed: %s", symbol, exc)
                continue

    # Telegram-quiet archive write so the dashboard feed sees this run.
    if fetched:
        body = (
            f"<b>📈 ARK 每日持仓 · {last_snap_date}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{' · '.join(fetched)} 当日 snapshot 已入库 · "
            f"{total_rows:,} position-rows"
        )
        # Emit chat_message inside the trace so the saved trace_json has
        # a complete reply event. (We do this AFTER the with-block above
        # already exited — but trace.events is captured by closure and
        # we want the rendering "reply" recorded.) Easiest: append the
        # event directly to trace.events; emit() is a no-op now that
        # TRACE_CTX is unbound.
        import time as _time
        trace.events.append({
            "type": "chat_message",
            "session_id": trace.session_id,
            "seq": len(trace.events) + 1,
            "ts_ms": int(_time.time() * 1000),
            "role": "bot",
            "text": body[:500],
        })
        Archive(agent="etf").save_text(
            body,
            tickers=fetched,
            trace_json=json.dumps(trace.events, ensure_ascii=False) if trace.events else None,
            title="ARK 每日持仓",
            expires_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        )

    logger.info(
        "Done. %d funds · %d position-rows saved.",
        total_funds, total_rows,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
