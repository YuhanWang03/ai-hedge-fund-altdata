"""Lab · 投资人委员会 — run the thirteen simulated investors from the workbench.

``POST /api/lab/committee`` takes a ticker source (explicit list, current
holdings, watchlist, or the output of the Lab screener), builds one
fundamentals snapshot per ticker, lets every selected persona vote, and
returns the matrix plus a confidence-weighted consensus.  Every run is
persisted (``v2/personas/store.py``) so the run log survives restarts and
forward returns can be back-filled later.

No LLM is involved.  Holdings get a transparent action label
(增持候选 / 持有 / 减持候选) derived from consensus, agreement and the
position's current weight — the rule is spelled out in :func:`_holding_action`.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_owner
from app.routers import workspace
from v2.personas.committee import CommitteeResult, TickerVerdict, run_committee
from v2.personas.data import adapt_client
from v2.personas.registry import PERSONAS, get_persona
from v2.personas.store import DEFAULT_PATH, PersonaStore

logger = logging.getLogger("web.committee")

router = APIRouter(prefix="/api/lab/committee", tags=["lab"], dependencies=[Depends(require_owner)])

MAX_TICKERS = 60
_STORE: PersonaStore | None = None

Source = Literal["tickers", "holdings", "watchlist", "screening"]


def _store() -> PersonaStore:
    global _STORE
    if _STORE is None:
        _STORE = PersonaStore(os.environ.get("WEB_PERSONAS_DB") or DEFAULT_PATH)
    return _STORE


class CommitteeInput(BaseModel):
    source: Source = "tickers"
    tickers: list[str] = Field(default_factory=list, max_length=MAX_TICKERS)
    personas: list[str] | None = None
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    top_n: int = Field(default=15, ge=1, le=MAX_TICKERS)
    use_cache: bool = True
    max_weight: float = Field(default=0.15, gt=0, le=1.0)
    screening: workspace.ScreeningInput | None = None


# --------------------------------------------------------------------------- inputs

def _resolve_personas(keys: list[str] | None) -> list[str]:
    if not keys:
        return list(PERSONAS)
    unknown = [k for k in keys if k not in PERSONAS]
    if unknown:
        raise ValueError(f"unknown persona: {', '.join(unknown)}")
    return list(dict.fromkeys(keys))


def _holdings() -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Long positions from Alpaca with their portfolio weight."""
    from v2.broker.alpaca_client import get_portfolio

    pf = get_portfolio()
    total = float((pf.get("account") or {}).get("portfolio_value") or 0.0)
    positions: dict[str, dict[str, Any]] = {}
    for p in pf.get("positions") or []:
        symbol = str(p.get("symbol") or "").upper()
        if not symbol or str(p.get("side", "long")).lower() != "long":
            continue
        mv = float(p.get("market_value") or 0.0)
        positions[symbol] = {
            "weight": (mv / total) if total > 0 else None,
            "market_value": mv,
            "current_price": float(p.get("current_price") or 0.0) or None,
            "unrealized_pl_pct": p.get("unrealized_pl_pct"),
        }
    return list(positions), positions


def _watchlist() -> list[str]:
    from v2.bot.state import watchlist_list

    return [str(item["ticker"]).upper() for item in watchlist_list()]


def _screened(body: workspace.ScreeningInput | None) -> tuple[list[str], dict[str, Any]]:
    result = workspace._run_screening(body or workspace.ScreeningInput())
    candidates = result.get("candidates") or []
    tickers = [str(c.get("ticker")).upper() for c in candidates if c.get("ticker")]
    summary = {
        "universe_size": result.get("universe_size"),
        "n_candidates": len(candidates),
        "date": result.get("date"),
        "candidates": [{"ticker": c.get("ticker"), "price": c.get("price"), "market_cap": c.get("market_cap"), "revenue_growth": c.get("revenue_growth"), "gross_margin": c.get("gross_margin")} for c in candidates],
    }
    return tickers, summary


@contextmanager
def _data_client() -> Iterator[Any]:
    """Production FDClient when available (VPS), else the stdlib HTTP client."""
    try:
        from v2.data import CachedFDClient  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — repo checkout has no v2/data
        yield adapt_client(None)
        return
    with CachedFDClient() as raw:
        yield adapt_client(raw)


# ---------------------------------------------------------------------------- rules

def _holding_action(v: TickerVerdict, weight: float | None, max_weight: float) -> tuple[str, str]:
    """Transparent rule: (label, why)."""
    if v.voters == 0:
        return "数据不足", "所有投资人弃权"
    if v.consensus >= 0.2 and v.agreement >= 0.5:
        if weight is not None and weight >= max_weight:
            return "持有", f"共识看多 {v.consensus:+.2f}，但权重 {weight:.1%} 已达上限 {max_weight:.0%}"
        return "增持候选", f"共识看多 {v.consensus:+.2f}，{v.bullish}/{v.voters} 位看多"
    if v.consensus <= -0.2:
        return "减持候选", f"共识看空 {v.consensus:+.2f}，{v.bearish}/{v.voters} 位看空"
    return "持有", f"意见分歧（{v.bullish}▲ {v.bearish}▼ {v.neutral}·），共识 {v.consensus:+.2f}"


def _persona_meta(keys: list[str]) -> list[dict[str, Any]]:
    out = []
    for key in keys:
        p = get_persona(key)
        out.append({"key": key, "name": p.name, "name_zh": p.name_zh, "style": p.style, "period": p.period, "lookback": p.lookback, "needs": sorted(p.needs)})
    return out


# ------------------------------------------------------------------------------ run

def _run(body: CommitteeInput) -> dict[str, Any]:
    keys = _resolve_personas(body.personas)
    positions: dict[str, dict[str, Any]] = {}
    screening: dict[str, Any] | None = None

    if body.source == "tickers":
        tickers = workspace._normalize_tickers(body.tickers, limit=MAX_TICKERS)
    elif body.source == "holdings":
        tickers, positions = _holdings()
        if not tickers:
            raise ValueError("no long positions in the account")
    elif body.source == "watchlist":
        tickers = _watchlist()
        if not tickers:
            raise ValueError("watchlist is empty")
    else:
        tickers, screening = _screened(body.screening)
        if not tickers:
            raise ValueError("screening returned no candidates")
    tickers = tickers[:MAX_TICKERS]

    store = _store()
    as_of = body.as_of
    cached = {}
    if body.use_cache:
        from datetime import date

        key_date = as_of or date.today().isoformat()
        for t in tickers:
            snap = store.cached_snapshot(t, key_date)
            if snap is not None:
                cached[t] = snap

    with _data_client() as client:
        result: CommitteeResult = run_committee(tickers, client, personas=keys, as_of=as_of, snapshots=cached, max_workers=4)

    for t, snap in result.snapshots.items():
        if t not in cached:
            store.save_snapshot(snap)

    payload = result.to_dict()
    payload["kind"] = "committee"
    payload["source"] = body.source
    payload["personas_meta"] = _persona_meta(keys)
    payload["cache_hits"] = sorted(cached)
    for v_dict, v in zip(payload["verdicts"], result.verdicts):
        pos = positions.get(v.ticker)
        if pos:
            label, why = _holding_action(v, pos.get("weight"), body.max_weight)
            v_dict["position"] = pos
            v_dict["action"] = label
            v_dict["action_reason"] = why
            v_dict["price"] = pos.get("current_price")
    if screening is not None:
        payload["screening"] = screening
    payload["top"] = [
        {"rank": v.rank, "ticker": v.ticker, "stance": v.stance, "consensus": round(v.consensus, 4), "bullish": v.bullish, "bearish": v.bearish, "neutral": v.neutral, "agreement": round(v.agreement, 4)}
        for v in result.top(body.top_n)
    ]
    payload["run_id"] = store.save_run(payload, source=body.source)
    workspace._remember_run("committee", payload)
    return payload


@router.post("")
async def run(body: CommitteeInput) -> dict:
    return await workspace._lab_call(_run, body)


@router.get("/personas")
async def personas() -> dict:
    return {"kind": "personas", "items": _persona_meta(list(PERSONAS))}


@router.get("/runs")
async def runs(limit: int = 50) -> dict:
    return {"kind": "committee_runs", "items": _store().list_runs(limit=max(1, min(limit, 200)))}


@router.get("/runs/{run_id}")
async def run_detail(run_id: str) -> dict:
    payload = _store().get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="committee run not found")
    return payload


@router.get("/scoreboard")
async def scoreboard() -> dict:
    """Per-persona hit rate once forward returns have been back-filled."""
    return {"kind": "scoreboard", "items": _store().persona_scoreboard()}
