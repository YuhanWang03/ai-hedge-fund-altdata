"""Thin aiosqlite wrapper. Single connection per process is fine for our
load — every guest query writes 4-5 rows, owner volume is by definition
small. WAL mode allows concurrent reads by the SSE handlers without
blocking writes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.config import SETTINGS


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Store:
    """Async SQLite store. One instance lives on app.state.store."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        schema_sql = _SCHEMA_PATH.read_text()
        await self._db.executescript(schema_sql)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store not opened"
        return self._db

    # ----- sessions ------------------------------------------------------

    async def create_session(
        self,
        *,
        session_id: str,
        text: str,
        client_kind: str,
        ip: str | None,
    ) -> None:
        now = int(time.time() * 1000)
        await self.db.execute(
            """
            INSERT INTO sessions
                (session_id, created_ms, updated_ms, text, client_kind, ip, status)
            VALUES (?, ?, ?, ?, ?, ?, 'in_progress')
            """,
            (session_id, now, now, text, client_kind, ip),
        )
        await self.db.commit()

    async def finalize_session(
        self,
        *,
        session_id: str,
        intent: str | None,
        args: dict[str, Any] | None,
        cache_key: str | None,
        status: str,
        reply_text: str | None,
        total_cost_usd: float,
        events: list[dict[str, Any]],
        expires_at_ms: int | None,
    ) -> None:
        now = int(time.time() * 1000)
        await self.db.execute(
            """
            UPDATE sessions
            SET updated_ms = ?,
                intent = ?,
                args_json = ?,
                cache_key = ?,
                status = ?,
                reply_text = ?,
                total_cost_usd = ?,
                events_json = ?,
                expires_at_ms = ?
            WHERE session_id = ?
            """,
            (
                now,
                intent,
                json.dumps(args) if args is not None else None,
                cache_key,
                status,
                reply_text,
                total_cost_usd,
                json.dumps(events),
                expires_at_ms,
                session_id,
            ),
        )
        await self.db.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    async def lookup_cache(
        self, cache_key: str, now_ms: int
    ) -> dict[str, Any] | None:
        """Most recent non-expired session for this cache_key, or None."""
        async with self.db.execute(
            """
            SELECT * FROM sessions
            WHERE cache_key = ? AND expires_at_ms > ? AND status = 'done'
            ORDER BY created_ms DESC
            LIMIT 1
            """,
            (cache_key, now_ms),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    async def insert_replay_session(
        self,
        *,
        session_id: str,
        source: dict[str, Any],
        text: str,
        client_kind: str,
        ip: str | None,
    ) -> None:
        """Create a session row that points at an already-cached one."""
        now = int(time.time() * 1000)
        await self.db.execute(
            """
            INSERT INTO sessions (
                session_id, cache_key, created_ms, updated_ms,
                intent, args_json, text, client_kind, ip,
                status, reply_text, total_cost_usd, events_json,
                cached_from, cached_at_ms, expires_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, 0, ?, ?, ?, ?)
            """,
            (
                session_id,
                source["cache_key"],
                now,
                now,
                source["intent"],
                source["args_json"],
                text,
                client_kind,
                ip,
                source["reply_text"],
                source["events_json"],
                source["session_id"],
                source["created_ms"],
                source["expires_at_ms"],
            ),
        )
        await self.db.commit()

    async def list_sessions(
        self, *, client_kind: str | None, ip: str | None, limit: int
    ) -> list[dict[str, Any]]:
        query = "SELECT session_id, created_ms, intent, text, status, total_cost_usd, cached_from FROM sessions"
        clauses: list[str] = []
        params: list[Any] = []
        if client_kind == "guest" and ip:
            clauses.append("ip = ?")
            params.append(ip)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_ms DESC LIMIT ?"
        params.append(limit)
        async with self.db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    # ----- rate limit ----------------------------------------------------

    async def rate_limit_check_and_increment(
        self, ip: str, now_ts: int, max_per_hour: int
    ) -> tuple[bool, int]:
        """Atomic check-and-increment. Returns (allowed, remaining)."""
        bucket = now_ts // 3600
        # INSERT OR IGNORE then UPDATE — both wrapped in one tx via commit.
        await self.db.execute(
            "INSERT OR IGNORE INTO rate_limit (ip, hour_bucket, count) VALUES (?, ?, 0)",
            (ip, bucket),
        )
        async with self.db.execute(
            "SELECT count FROM rate_limit WHERE ip = ? AND hour_bucket = ?",
            (ip, bucket),
        ) as cur:
            row = await cur.fetchone()
        current = row["count"] if row else 0
        if current >= max_per_hour:
            await self.db.commit()
            return False, 0
        await self.db.execute(
            "UPDATE rate_limit SET count = count + 1 WHERE ip = ? AND hour_bucket = ?",
            (ip, bucket),
        )
        await self.db.commit()
        return True, max_per_hour - (current + 1)

    async def rate_limit_status(self, ip: str, now_ts: int, max_per_hour: int) -> int:
        bucket = now_ts // 3600
        async with self.db.execute(
            "SELECT count FROM rate_limit WHERE ip = ? AND hour_bucket = ?",
            (ip, bucket),
        ) as cur:
            row = await cur.fetchone()
        used = row["count"] if row else 0
        return max(0, max_per_hour - used)

    # ----- daily budget --------------------------------------------------

    async def budget_reserve(self, day_bucket: int, amount: float, cap: float) -> tuple[bool, float]:
        """Atomic check-and-add. Returns (allowed, new_spent_total)."""
        await self.db.execute(
            "INSERT OR IGNORE INTO daily_budget (day_bucket, spent_usd) VALUES (?, 0)",
            (day_bucket,),
        )
        async with self.db.execute(
            "SELECT spent_usd FROM daily_budget WHERE day_bucket = ?", (day_bucket,)
        ) as cur:
            row = await cur.fetchone()
        spent = row["spent_usd"] if row else 0.0
        if spent + amount > cap:
            await self.db.commit()
            return False, spent
        new_spent = spent + amount
        await self.db.execute(
            "UPDATE daily_budget SET spent_usd = ? WHERE day_bucket = ?",
            (new_spent, day_bucket),
        )
        await self.db.commit()
        return True, new_spent

    async def budget_settle(self, day_bucket: int, delta: float) -> float:
        """Add delta (positive or negative) to today's spend. Returns new total."""
        await self.db.execute(
            "INSERT OR IGNORE INTO daily_budget (day_bucket, spent_usd) VALUES (?, 0)",
            (day_bucket,),
        )
        await self.db.execute(
            "UPDATE daily_budget SET spent_usd = MAX(0, spent_usd + ?) WHERE day_bucket = ?",
            (delta, day_bucket),
        )
        async with self.db.execute(
            "SELECT spent_usd FROM daily_budget WHERE day_bucket = ?", (day_bucket,)
        ) as cur:
            row = await cur.fetchone()
        await self.db.commit()
        return row["spent_usd"] if row else 0.0

    async def budget_status(self, day_bucket: int) -> float:
        async with self.db.execute(
            "SELECT spent_usd FROM daily_budget WHERE day_bucket = ?", (day_bucket,)
        ) as cur:
            row = await cur.fetchone()
        return row["spent_usd"] if row else 0.0


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def open_store_factory() -> Store:
    return Store(SETTINGS.db_path)
