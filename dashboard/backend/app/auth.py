"""Caller identification + guest gating.

Two kinds of caller:
- Owner: presents a valid X-Owner-Token header. No budget, no rate limit,
  no intent restrictions, no cache use.
- Guest: anyone else. Subject to per-IP rate limit, global daily budget,
  guest intent whitelist, and the replay cache.

This module deliberately does not enforce budget/rate limits — that is
budget.py's job. It only resolves the caller's identity and surfaces the
guest whitelist for downstream checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import Header, HTTPException, Request

from app.config import SETTINGS


Kind = Literal["owner", "guest"]


@dataclass(frozen=True)
class Caller:
    kind: Kind
    ip: str

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"

    def may_use_intent(self, intent_name: str | None) -> bool:
        if self.is_owner:
            return True
        if intent_name is None:
            return False
        return intent_name in SETTINGS.guest_intent_whitelist


def _client_ip(request: Request) -> str:
    # Honor X-Forwarded-For when set by the reverse proxy (nginx).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is None:
        return "unknown"
    return request.client.host


async def resolve_caller(
    request: Request,
    x_owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
) -> Caller:
    """FastAPI dependency. Identifies the caller; does not enforce limits."""
    ip = _client_ip(request)
    if (
        SETTINGS.owner_token
        and x_owner_token is not None
        and x_owner_token == SETTINGS.owner_token
    ):
        return Caller(kind="owner", ip=ip)
    if x_owner_token is not None and x_owner_token != SETTINGS.owner_token:
        # Wrong token presented — treat as a misconfigured owner attempt,
        # not silent guest fallback, so the user notices.
        raise HTTPException(status_code=401, detail="invalid_owner_token")
    return Caller(kind="guest", ip=ip)
