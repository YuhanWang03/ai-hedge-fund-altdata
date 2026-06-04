"""Tests for v2/archive/store.py schema migrations.

Specifically guards against the bug where the priority columns weren't
being added to pre-Phase-0 production archive.db files: simulate the
old-schema starting state and assert _ensure_phase2_columns brings it
all the way up.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


@pytest.fixture
def fresh_archive(tmp_path, monkeypatch):
    """Point v2.archive.store at a temp DB and return the (Archive, path)."""
    db = tmp_path / "archive.db"
    img = tmp_path / "img"
    # Make sure v2 is importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from v2.archive import store
    monkeypatch.setattr(store, "_DB_PATH", db)
    monkeypatch.setattr(store, "_IMG_ROOT", img)
    return store, db


def _cols(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(pushes)")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bug regression: pre-Phase-0 schema → Phase 0 must add all 3 priority cols.
# ---------------------------------------------------------------------------

def test_pre_phase0_schema_upgrade_adds_priority_columns(fresh_archive):
    """Simulate the schema that shipped with Phase 2 (trace_json/title/
    expires_at present, no priority cols). Booting Archive() must migrate
    forward to include importance_score / priority_tier / priority_reasons.
    """
    store, db = fresh_archive

    # Seed the Phase 2 schema explicitly — no priority columns.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE pushes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            agent TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            text_html TEXT,
            image_path TEXT,
            tickers TEXT,
            trace_json TEXT,
            title TEXT,
            expires_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    assert "priority_tier" not in _cols(db)

    # Boot Archive — _ensure_phase2_columns should ALTER the missing
    # columns in.
    store.Archive("test")

    after = _cols(db)
    assert {"importance_score", "priority_tier", "priority_reasons"} <= after, (
        f"priority columns missing after migration: {after}"
    )


def test_original_schema_upgrade_adds_all_six_columns(fresh_archive):
    """Even older starting point: pre-Phase-2 (no trace_json either).
    Migration must catch up the whole chain."""
    store, db = fresh_archive

    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE pushes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            agent TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            text_html TEXT,
            image_path TEXT,
            tickers TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    store.Archive("test")

    after = _cols(db)
    expected = {
        "trace_json", "title", "expires_at",
        "importance_score", "priority_tier", "priority_reasons",
    }
    assert expected <= after, f"missing: {expected - after}"


def test_idempotent_second_init(fresh_archive):
    """Running Archive() a second time on an already-migrated DB must
    be a no-op — not raise, not duplicate-add."""
    store, _ = fresh_archive
    store.Archive("first")
    store.Archive("second")
    store.Archive("third")  # should still succeed


def test_already_phase0_schema_passes_through(fresh_archive):
    """If the DB ALREADY has all 6 columns (e.g., manually ALTERed in
    production), Archive() must still init cleanly."""
    store, db = fresh_archive

    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE pushes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            agent TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            text_html TEXT,
            image_path TEXT,
            tickers TEXT,
            trace_json TEXT,
            title TEXT,
            expires_at TEXT,
            importance_score INTEGER,
            priority_tier TEXT,
            priority_reasons TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    store.Archive("test")   # must not raise


def test_concurrent_init_does_not_corrupt_migration(fresh_archive):
    """Multiple Archive() inits racing (e.g. cron + bot + dashboard
    coming up together) must converge to all-columns-present without
    raising 'duplicate column'."""
    import threading
    store, db = fresh_archive

    errors: list[Exception] = []

    def boot():
        try:
            store.Archive("racer")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"race produced errors: {errors[:3]}"
    after = _cols(db)
    required = {
        "trace_json", "title", "expires_at",
        "importance_score", "priority_tier", "priority_reasons",
    }
    assert required <= after, f"missing after race: {required - after}"


def test_save_text_after_migration_uses_new_columns(fresh_archive):
    """End-to-end: after migration, save_text with priority kwargs must
    successfully write all priority fields."""
    store, db = fresh_archive
    archive = store.Archive("smoke")
    row_id = archive.save_text(
        "hello",
        priority_tier="P0",
        importance_score=85,
        priority_reasons="base=85,+15_held",
    )
    assert row_id > 0

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT priority_tier, importance_score, priority_reasons "
        "FROM pushes WHERE id = ?", (row_id,),
    ).fetchone()
    conn.close()
    assert row == ("P0", 85, "base=85,+15_held")
