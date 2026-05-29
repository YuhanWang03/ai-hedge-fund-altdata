"""SQLite-backed archive of all Telegram pushes.

Every message + image gets stored locally before being sent to Telegram, so:
- We never lose data even if a Telegram push fails
- We can query history offline ("when did we last mention NVDA?")
- Phase C's RAG memory will index this DB for semantic retrieval

Image files live alongside the DB under data/images/YYYY-MM-DD/.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _PROJECT_ROOT / "data" / "archive.db"
_IMG_ROOT = _PROJECT_ROOT / "data" / "images"

# Tickers appear inside <b>…</b> blocks. The old anchor `<b>TICKER</b>` only
# matched bare-ticker headers (e.g. screening cards). Anomaly cards have
# composite bolded headers like `<b>🚨 异动 · NVDA · 2026-05-27</b>` where
# the ticker is just one word among many — so we now scan every bold block
# for ticker-shaped words and filter out common false positives.
_BOLD_BLOCK_PATTERN = re.compile(r"<b>([^<]+)</b>")
_TICKER_WORD = re.compile(r"\b([A-Z]{2,5})\b")
_TICKER_BLOCKLIST = frozenset({
    "AI", "API", "CEO", "CFO", "COO", "CIO", "CTO", "EPS", "ETF", "EU", "EUR",
    "FY", "GDP", "GPU", "HBM", "ID", "IDE", "IT", "LLC", "NA", "NLP", "NYC",
    "PR", "Q1", "Q2", "Q3", "Q4", "RAG", "ROE", "ROI", "TTM", "UI", "UK",
    "US", "USA", "USD", "VS", "WSB", "YOY",
})


def _extract_tickers(text: str) -> set[str]:
    """Find ticker-shaped tokens in every <b>…</b> block, with a blocklist."""
    tickers: set[str] = set()
    for block in _BOLD_BLOCK_PATTERN.findall(text):
        for match in _TICKER_WORD.findall(block):
            if match in _TICKER_BLOCKLIST:
                continue
            tickers.add(match)
    return tickers

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pushes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    agent           TEXT NOT NULL,
    msg_type        TEXT NOT NULL,
    text_html       TEXT,
    image_path      TEXT,
    tickers         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pushes_ts     ON pushes(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pushes_agent  ON pushes(agent);
CREATE INDEX IF NOT EXISTS idx_pushes_tickers ON pushes(tickers);
"""


class Archive:
    """Per-agent archive client. Pass to TelegramNotifier(archive=...).

    A single Archive instance is tied to one agent label (e.g. "screen") so
    each script knows what to tag its rows with. The underlying DB is shared.
    """

    def __init__(self, agent: str) -> None:
        self._agent = agent
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _IMG_ROOT.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(_DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def save_text(
        self,
        text: str,
        *,
        tickers: Iterable[str] = (),
    ) -> int:
        """Archive a text push. Returns the row id (also useful as a handle)."""
        return self._insert(
            msg_type="text",
            text_html=text,
            image_path=None,
            tickers=self._resolve_tickers(text, tickers),
        )

    def save_photo(
        self,
        image: bytes,
        caption: str = "",
        *,
        tickers: Iterable[str] = (),
    ) -> int:
        """Archive a photo push (image written to disk + caption to DB)."""
        ts_iso = _now_iso()
        date_dir = ts_iso[:10]  # YYYY-MM-DD
        target_dir = _IMG_ROOT / date_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        # Deterministic-ish filename, never collides
        primary_ticker = (
            _first_ticker(tickers)
            or _first_ticker(_extract_tickers(caption or ""))
            or "img"
        )
        suffix = uuid.uuid4().hex[:8]
        img_path = target_dir / f"{self._agent}_{primary_ticker}_{suffix}.png"
        try:
            img_path.write_bytes(image)
        except OSError as exc:
            logger.warning("Failed to write image %s: %s", img_path, exc)
            img_path = None

        rel_path = (
            str(img_path.relative_to(_PROJECT_ROOT))
            if img_path is not None else None
        )
        return self._insert(
            msg_type="photo",
            text_html=caption or None,
            image_path=rel_path,
            tickers=self._resolve_tickers(caption or "", tickers),
            ts_override=ts_iso,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _insert(
        self,
        *,
        msg_type: str,
        text_html: str | None,
        image_path: str | None,
        tickers: str | None,
        ts_override: str | None = None,
    ) -> int:
        ts = ts_override or _now_iso()
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    """INSERT INTO pushes
                       (ts, agent, msg_type, text_html, image_path, tickers)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ts, self._agent, msg_type, text_html, image_path, tickers),
                )
                return cur.lastrowid or 0
        except sqlite3.Error as exc:
            logger.warning("Archive insert failed (%s): %s", self._agent, exc)
            return 0

    def _resolve_tickers(
        self,
        text: str,
        explicit: Iterable[str],
    ) -> str | None:
        """Union explicit-passed tickers with any auto-detected from HTML."""
        found: set[str] = set(t.upper() for t in explicit if t)
        if text:
            found.update(_extract_tickers(text))
        return ",".join(sorted(found)) if found else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _first_ticker(it) -> str | None:
    for x in it:
        if x:
            return str(x).upper()
    return None
