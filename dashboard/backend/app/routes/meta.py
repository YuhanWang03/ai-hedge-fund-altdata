"""/api/health, /api/budget/status, /api/help."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.auth import Caller, resolve_caller
from app.budget import status as budget_status_fn
from app.config import SETTINGS

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    return {
        "ok": True,
        "observability_hooks": getattr(
            request.app.state, "installed_hooks", []
        ),
    }


@router.get("/budget/status")
async def budget_status(
    request: Request, caller: Caller = Depends(resolve_caller)
) -> dict:
    return await budget_status_fn(request.app.state.store, caller)


@router.get("/help")
async def help_endpoint(caller: Caller = Depends(resolve_caller)) -> dict:
    return {
        "kind": caller.kind,
        "guest_intent_whitelist": sorted(SETTINGS.guest_intent_whitelist),
        "cache_ttl_seconds": SETTINGS.cache_ttl_seconds,
        "rate_limit": (
            None if caller.is_owner else f"{SETTINGS.per_ip_hourly_limit}/hour/IP"
        ),
        "daily_budget_usd": (
            None if caller.is_owner else SETTINGS.daily_budget_usd
        ),
    }
