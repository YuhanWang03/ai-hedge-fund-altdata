"""Shared pytest fixtures.

Sets env vars BEFORE any `app.*` module imports so app.config picks them up
on its initial (and only) load. Tests then receive a per-test temp DB path
via monkeypatching the SETTINGS dataclass attribute directly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Env vars must be set before any `app.*` import.
os.environ.setdefault("DASHBOARD_OWNER_TOKEN", "test-owner-token")
os.environ.setdefault("DASHBOARD_DAILY_BUDGET_USD", "0.30")
os.environ.setdefault("DASHBOARD_PER_IP_HOURLY_LIMIT", "5")
os.environ.setdefault("DASHBOARD_DB_PATH", "/tmp/_dashboard_test_default.db")

# Make `app.*` importable.
sys.path.insert(0, str(Path(__file__).parent.parent))
# Make `v2.*` importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import pytest  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path):
    """Point SETTINGS.db_path at a fresh per-test file."""
    from app.config import SETTINGS
    db = tmp_path / "test_dashboard.db"
    original = SETTINGS.db_path
    # SETTINGS is a frozen dataclass — bypass frozen via object.__setattr__.
    object.__setattr__(SETTINGS, "db_path", str(db))
    try:
        yield str(db)
    finally:
        object.__setattr__(SETTINGS, "db_path", original)


@pytest.fixture
async def store(tmp_db):
    from app.db.store import Store
    s = Store(tmp_db)
    await s.open()
    try:
        yield s
    finally:
        await s.close()
