"""SQLite persistence for committee runs, persona signals and snapshot cache.

Three tables, one file (``data/personas.db`` by default, next to the other
v2 databases):

* ``committee_runs`` — one row per run: who was asked, about what, the
  full result JSON.  This is what the Lab's run log reads.
* ``persona_signals`` — one row per (run, ticker, persona).  Flat columns
  for signal / confidence / score so a forward-return backfill and a
  per-persona hit-rate query are plain SQL later; ``fwd_*`` start NULL.
* ``snapshots`` — the fundamentals snapshot per (ticker, as_of), keyed by
  content hash, so a second run on the same day costs no API calls.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from v2.personas.snapshot import PersonaSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = PROJECT_ROOT / "data" / "personas.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS committee_runs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  as_of TEXT NOT NULL,
  source TEXT NOT NULL,
  tickers_json TEXT NOT NULL,
  personas_json TEXT NOT NULL,
  n_tickers INTEGER NOT NULL,
  elapsed_s REAL NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_committee_runs_created ON committee_runs(created_at DESC);
CREATE TABLE IF NOT EXISTS persona_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  as_of TEXT NOT NULL,
  ticker TEXT NOT NULL,
  persona TEXT NOT NULL,
  signal TEXT NOT NULL,
  confidence INTEGER NOT NULL,
  score REAL NOT NULL,
  max_score REAL NOT NULL,
  margin_of_safety REAL,
  abstained INTEGER NOT NULL DEFAULT 0,
  snapshot_hash TEXT,
  reasoning TEXT,
  facts_json TEXT,
  price_at REAL,
  fwd_1m REAL,
  fwd_3m REAL,
  UNIQUE(run_id, ticker, persona)
);
CREATE INDEX IF NOT EXISTS idx_persona_signals_ticker ON persona_signals(ticker, as_of DESC);
CREATE INDEX IF NOT EXISTS idx_persona_signals_persona ON persona_signals(persona, as_of DESC);
CREATE TABLE IF NOT EXISTS snapshots (
  ticker TEXT NOT NULL,
  as_of TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (ticker, as_of)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PersonaStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- runs ---------------------------------------------------------------------

    def save_run(self, result: dict[str, Any], *, source: str, run_id: str | None = None) -> str:
        """Persist a ``CommitteeResult.to_dict()`` (plus any extras) and its signals."""
        run_id = run_id or uuid.uuid4().hex[:12]
        now = utc_now()
        verdicts = result.get("verdicts") or []
        tickers = [v["ticker"] for v in verdicts]
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO committee_runs (id, created_at, as_of, source, tickers_json, personas_json, n_tickers, elapsed_s, result_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id, now, result.get("as_of", ""), source, json.dumps(tickers),
                    json.dumps(result.get("personas") or []), len(tickers), float(result.get("elapsed_s") or 0.0),
                    json.dumps({**result, "run_id": run_id, "source": source, "created_at": now}, ensure_ascii=False, default=str),
                ),
            )
            rows = []
            for v in verdicts:
                for s in v.get("signals") or []:
                    rows.append((
                        run_id, now, s.get("as_of", ""), v["ticker"], s["persona"], s["signal"], int(s["confidence"]),
                        float(s["score"]), float(s["max_score"]), s.get("margin_of_safety"), 1 if s.get("abstained") else 0,
                        s.get("snapshot_hash"), s.get("reasoning"), json.dumps(s.get("facts") or {}, ensure_ascii=False, default=str),
                        v.get("price"),
                    ))
            conn.executemany(
                "INSERT OR REPLACE INTO persona_signals (run_id, created_at, as_of, ticker, persona, signal, confidence, score, max_score, margin_of_safety, abstained, snapshot_hash, reasoning, facts_json, price_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT result_json FROM committee_runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, created_at, as_of, source, tickers_json, personas_json, n_tickers, elapsed_s FROM committee_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": r["id"], "created_at": r["created_at"], "as_of": r["as_of"], "source": r["source"],
                "tickers": json.loads(r["tickers_json"]), "personas": json.loads(r["personas_json"]),
                "n_tickers": r["n_tickers"], "elapsed_s": r["elapsed_s"],
            }
            for r in rows
        ]

    def update_signal_narrative(self, run_id: str, ticker: str, persona: str, narrative: str, grounded: bool | None) -> bool:
        """Write an LLM narrative into a stored run's result JSON. Returns False if not found."""
        with self._conn() as conn:
            row = conn.execute("SELECT result_json FROM committee_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return False
            payload = json.loads(row["result_json"])
            hit = False
            for v in payload.get("verdicts") or []:
                if v.get("ticker") != ticker:
                    continue
                for sig in v.get("signals") or []:
                    if sig.get("persona") == persona:
                        sig["narrative"] = narrative
                        sig["narrative_grounded"] = grounded
                        hit = True
            if hit:
                conn.execute("UPDATE committee_runs SET result_json = ? WHERE id = ?", (json.dumps(payload, ensure_ascii=False, default=str), run_id))
            return hit

    # -- signals ------------------------------------------------------------------

    def latest_signals(self, ticker: str, limit: int = 13) -> list[dict[str, Any]]:
        """Most recent signal per persona for one ticker."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT persona, signal, confidence, score, max_score, margin_of_safety, abstained, as_of, run_id, reasoning
                   FROM persona_signals WHERE ticker = ? AND id IN (
                     SELECT MAX(id) FROM persona_signals WHERE ticker = ? GROUP BY persona
                   ) ORDER BY persona LIMIT ?""",
                (ticker.upper(), ticker.upper(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def signals_awaiting_forward_returns(self, *, older_than_days: int, column: str = "fwd_1m", today: date | None = None) -> list[dict[str, Any]]:
        """Signals old enough to be scored, whose ``column`` is still NULL."""
        if column not in ("fwd_1m", "fwd_3m"):
            raise ValueError("column must be fwd_1m or fwd_3m")
        base = today or datetime.now(timezone.utc).date()
        cutoff = (base - timedelta(days=older_than_days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id, ticker, as_of, persona, signal, price_at FROM persona_signals WHERE {column} IS NULL AND abstained = 0 AND as_of <= ? ORDER BY as_of",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_forward_return(self, signal_id: int, *, column: str, value: float) -> None:
        if column not in ("fwd_1m", "fwd_3m"):
            raise ValueError("column must be fwd_1m or fwd_3m")
        with self._conn() as conn:
            conn.execute(f"UPDATE persona_signals SET {column} = ? WHERE id = ?", (value, signal_id))

    def persona_scoreboard(self) -> list[dict[str, Any]]:
        """Hit rate per persona over signals that have a 1-month forward return."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT persona,
                          COUNT(*) AS n,
                          SUM(CASE WHEN (signal = 'bullish' AND fwd_1m > 0) OR (signal = 'bearish' AND fwd_1m < 0) THEN 1 ELSE 0 END) AS hits,
                          AVG(CASE WHEN signal = 'bullish' THEN fwd_1m WHEN signal = 'bearish' THEN -fwd_1m END) AS avg_directional_1m
                   FROM persona_signals WHERE fwd_1m IS NOT NULL AND signal != 'neutral'
                   GROUP BY persona ORDER BY hits * 1.0 / COUNT(*) DESC"""
            ).fetchall()
        return [
            {"persona": r["persona"], "n": r["n"], "hits": r["hits"], "hit_rate": (r["hits"] / r["n"]) if r["n"] else None, "avg_directional_1m": r["avg_directional_1m"]}
            for r in rows
        ]

    def signal_counts(self) -> dict[str, int]:
        """Vote bookkeeping for the scoreboard: totals, scored, pending per horizon."""
        today = datetime.now(timezone.utc).date()
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM persona_signals WHERE abstained = 0").fetchone()[0]
            scored_1m = conn.execute("SELECT COUNT(*) FROM persona_signals WHERE fwd_1m IS NOT NULL").fetchone()[0]
            scored_3m = conn.execute("SELECT COUNT(*) FROM persona_signals WHERE fwd_3m IS NOT NULL").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM committee_runs").fetchone()[0]
            tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM persona_signals").fetchone()[0]
        return {
            "runs": runs, "tickers": tickers, "votes": total,
            "scored_1m": scored_1m, "scored_3m": scored_3m,
            "due_1m": len(self.signals_awaiting_forward_returns(older_than_days=30, column="fwd_1m", today=today)),
            "due_3m": len(self.signals_awaiting_forward_returns(older_than_days=91, column="fwd_3m", today=today)),
        }

    # -- snapshot cache -----------------------------------------------------------

    def cached_snapshot(self, ticker: str, as_of: str, *, max_age_hours: float = 24.0) -> PersonaSnapshot | None:
        with self._conn() as conn:
            row = conn.execute("SELECT fetched_at, payload_json FROM snapshots WHERE ticker = ? AND as_of = ?", (ticker.upper(), as_of)).fetchone()
        if not row:
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
            return None
        return PersonaSnapshot.from_dict(json.loads(row["payload_json"]))

    def save_snapshot(self, snap: PersonaSnapshot) -> None:
        if not snap.has_fundamentals:
            return  # never cache an empty fetch; the next run should retry
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (ticker, as_of, content_hash, fetched_at, payload_json) VALUES (?,?,?,?,?)",
                (snap.ticker, snap.as_of, snap.content_hash, snap.fetched_at or utc_now(), json.dumps(snap.to_dict(), default=str)),
            )
