"""Detect anomalies on TECH_30 and push attributed alerts to Telegram.

On quiet days this script silently exits — no message is sent.

Usage:
    poetry run python scripts/anomaly_to_telegram.py
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

# Surface WARN+ logs from v2.monitoring so we can see Tavily-empty cases.
logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")

from v2.archive import Archive
from v2.data import CachedFDClient
from v2.memory import AnomalyMemory
from v2.monitoring import DEFAULT_CONFIG, attribute, run_monitoring
from v2.reporting import (
    TelegramNotifier,
    format_anomaly_alert,
    notify_on_error,
    render_price_sparkline,
)
from v2.screening import TECH_30

load_dotenv()


@notify_on_error("Anomaly Monitor")
def main() -> None:
    print(f"Monitoring {len(TECH_30)} tickers...")
    # Keep FD context open through attribution so we can fetch company names
    # for the entity-filter step.
    with CachedFDClient() as fd:
        anomalies = run_monitoring(TECH_30, fd, DEFAULT_CONFIG)

        if not anomalies:
            print("No anomalies on the latest trading day — staying silent.")
            return

        print(
            f"Detected {len(anomalies)} anomalies: "
            f"{', '.join(a.ticker + str(a.flags) for a in anomalies)}"
        )

        # Phase C: long-term anomaly memory (ChromaDB + OpenAI embeddings).
        # Best-effort — if it fails to initialize, we skip historical context.
        try:
            memory = AnomalyMemory()
        except Exception as exc:
            print(f"  [warn] AnomalyMemory init failed: {exc}; running without RAG")
            memory = None

        notifier = TelegramNotifier(archive=Archive(agent="anomaly"))
        for anomaly in anomalies:
            print(f"  Attributing {anomaly.ticker}...")
            attribute(anomaly, fd_client=fd, memory=memory)
            chart = render_price_sparkline(
                anomaly.recent_prices,
                title=f"{anomaly.ticker} · 最近 {len(anomaly.recent_prices)} 日",
            )
            notifier.send_photo(chart, caption=format_anomaly_alert(anomaly))
            print(
                f"    pushed ({len(anomaly.reasons)} reasons, "
                f"{len(anomaly.next_steps)} next-steps, "
                f"⛔ filtered {anomaly.filtered_count}, "
                f"🧠 {len(anomaly.historical_context)} historical, "
                f"{anomaly.llm_tokens} tokens)"
            )

    print("Done.")


if __name__ == "__main__":
    main()
