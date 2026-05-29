"""Dashboard backend configuration loaded from environment variables.

All settings are read once at startup. The dashboard never accepts owner
credentials in the URL or body — they always travel via X-Owner-Token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Authentication
    owner_token: str

    # Budget enforcement (guest only)
    daily_budget_usd: float
    per_ip_hourly_limit: int

    # Guest-allowed intents. Owner sees everything.
    guest_intent_whitelist: frozenset[str]

    # Cache TTLs in seconds, keyed by intent name.
    cache_ttl_seconds: dict[str, int]
    default_cache_ttl_seconds: int

    # Filesystem
    db_path: str
    hedge_fund_repo_path: str

    # Server
    host: str
    port: int
    cors_origins: tuple[str, ...]


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"required environment variable {name} not set")
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def load_settings() -> Settings:
    return Settings(
        owner_token=_env("DASHBOARD_OWNER_TOKEN", required=False),
        daily_budget_usd=_env_float("DASHBOARD_DAILY_BUDGET_USD", 0.30),
        per_ip_hourly_limit=_env_int("DASHBOARD_PER_IP_HOURLY_LIMIT", 5),
        guest_intent_whitelist=frozenset({
            "explain_move",
            "summary",
            "chain",
            "thirteen_f",
            "holders_view",
            "etf_view",
            "find_anomalies",
            "settings",
        }),
        cache_ttl_seconds={
            "explain_move": 5 * 60,
            "summary": 10 * 60,
            "chain": 30 * 60,
            "thirteen_f": 30 * 60,
            "holders_view": 30 * 60,
            "etf_view": 60 * 60,
            "find_anomalies": 10 * 60,
            "settings": 60 * 60,
        },
        default_cache_ttl_seconds=5 * 60,
        db_path=_env("DASHBOARD_DB_PATH", "/home/user/ai-hedge-fund-altdata/dashboard/data/dashboard.db"),
        hedge_fund_repo_path=_env(
            "HEDGE_FUND_REPO_PATH", "/home/user/ai-hedge-fund-altdata"
        ),
        host=_env("DASHBOARD_HOST", "127.0.0.1"),
        port=_env_int("DASHBOARD_PORT", 8001),
        cors_origins=tuple(
            o.strip() for o in _env("DASHBOARD_CORS_ORIGINS", "*").split(",") if o.strip()
        ),
    )


SETTINGS: Settings = load_settings()
