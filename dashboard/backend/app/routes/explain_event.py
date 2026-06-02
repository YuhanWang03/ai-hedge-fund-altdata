"""LLM-generated natural-language explanations for individual trace events.

Owner-only endpoint that complements the static 5-field 📖 解析 with a
1-3 sentence prose translation written by DeepSeek. Results are cached
in-memory (LRU, max 500) so the second time a similar event shows up
the response is free and instant.

Cache key intentionally drops dynamic fields (ticker, tokens, elapsed_ms,
seq, ts_ms) — the natural-language description of "what an FD price
fetch means" doesn't change between NVDA and AAPL. We're caching the
shape of the event, not its instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import Caller, resolve_caller
from app.config import SETTINGS
from app.runner.event_explanations import lookup as lookup_explanation

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------

class ExplainEventRequest(BaseModel):
    event: dict[str, Any]
    intent: Optional[str] = None


class ExplainEventResponse(BaseModel):
    explanation: str
    cached: bool


# ---------------------------------------------------------------------------
# In-memory LRU cache (process-local; rebuild on restart is fine)
# ---------------------------------------------------------------------------

_CACHE_MAX = 500
_cache: "OrderedDict[tuple, str]" = OrderedDict()
_cache_lock = asyncio.Lock()


def _cache_key(event: dict[str, Any]) -> tuple:
    """Cache by event shape, NOT by instance.

    Two FD get_prices calls for NVDA and AAPL share the same key — the
    natural-language description is identical apart from ticker, and
    the prompt template injects ticker textually for the LLM so we
    don't need a per-ticker entry.
    """
    return (
        str(event.get("type") or ""),
        str(event.get("op") or ""),
        str(event.get("fn") or ""),
        str(event.get("provider") or ""),
        str(event.get("card") or ""),
        str(event.get("role") or ""),
    )


async def _cache_get(key: tuple) -> Optional[str]:
    async with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


async def _cache_put(key: tuple, value: str) -> None:
    async with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _cache_size() -> int:
    return len(_cache)


def _cache_clear() -> None:
    """Test helper — wipes the LRU."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_PROMPT_RESERVED = {
    "type", "provider", "fn", "op", "card", "role", "ticker",
    "session_id", "seq", "ts_ms", "explanation", "replayed",
    "cached_from", "cached_at_ms",
}


def _build_prompt(event: dict[str, Any], intent: Optional[str], expl: dict[str, str]) -> str:
    rest = {k: v for k, v in event.items() if k not in _PROMPT_RESERVED}
    rest_json = json.dumps(rest, ensure_ascii=False, default=str)

    return (
        "你是一个面向产品经理 / 非技术 VP 的「系统观测翻译官」。\n"
        "下面是 AI Hedge Fund 系统刚发生的一个事件，你的任务是把它\n"
        "用一句到三句通俗易懂的中文讲明白。\n"
        "\n"
        "【风格规则】\n"
        "- 重点：把内容讲明白，不要装专业\n"
        "- 长度不限但别太长（一句到三句之间）\n"
        "- 遇到技术词加普通话注解，例如「向量化嵌入」要说「把文本变成数字坐标」\n"
        "- 用「我们」或省主语，不要「系统」「程序」\n"
        "- 多用动词，少被动\n"
        "- 不要复述事件元数据（type=api 这种），讲它「在干嘛」\n"
        "\n"
        "【事件原始数据】\n"
        f"type: {event.get('type', '-')}\n"
        f"provider: {event.get('provider', '-')}\n"
        f"fn: {event.get('fn', '-')}\n"
        f"op: {event.get('op', '-')}\n"
        f"card: {event.get('card', '-')}\n"
        f"role: {event.get('role', '-')}\n"
        f"ticker: {event.get('ticker', '-')}\n"
        f"其他字段：{rest_json}\n"
        "\n"
        "【现有 5 字段静态注解】（参考）\n"
        f"来源: {expl.get('source', '(无)')}\n"
        f"方式: {expl.get('how', '(无)')}\n"
        f"内容: {expl.get('what', '(无)')}\n"
        f"存储: {expl.get('store', '(无)')}\n"
        f"下一步: {expl.get('next', '(无)')}\n"
        "\n"
        f"【当前查询 intent】 {intent or '(无)'}\n"
        "\n"
        "现在用通俗中文写一段解析："
    )


# ---------------------------------------------------------------------------
# DeepSeek call (httpx — avoids pulling langchain into the backend env)
# ---------------------------------------------------------------------------

_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


async def _call_deepseek(prompt: str, *, api_key: str) -> str:
    """Synchronous-style async call to DeepSeek chat. Returns the
    assistant content stripped of leading/trailing whitespace.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_DEEPSEEK_URL, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    return str(data["choices"][0]["message"]["content"]).strip()


# Test seam — production calls _call_deepseek directly. Tests patch this
# attribute on the module to inject canned responses without monkeypatching
# httpx itself.
_llm_call_async = _call_deepseek


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/explain_event_llm", response_model=ExplainEventResponse)
async def explain_event_llm(
    payload: ExplainEventRequest,
    caller: Caller = Depends(resolve_caller),
) -> ExplainEventResponse:
    if caller.kind != "owner":
        raise HTTPException(status_code=403, detail="owner-only feature")

    event = payload.event or {}
    intent = payload.intent

    key = _cache_key(event)
    cached_value = await _cache_get(key)
    if cached_value is not None:
        return ExplainEventResponse(explanation=cached_value, cached=True)

    if not SETTINGS.deepseek_api_key:
        # Endpoint is wired but no API key on this host. Fail loudly so
        # the operator knows to set DEEPSEEK_API_KEY.
        raise HTTPException(
            status_code=503,
            detail="DeepSeek API key not configured (set DEEPSEEK_API_KEY)",
        )

    expl = lookup_explanation(event) or {}
    prompt = _build_prompt(event, intent, expl)

    try:
        # Reads the module-level binding so tests can swap it.
        text = await _llm_call_async(prompt, api_key=SETTINGS.deepseek_api_key)
    except httpx.HTTPError as exc:
        logger.warning("explain_event_llm DeepSeek call failed: %s", exc)
        raise HTTPException(status_code=502, detail="LLM call failed")

    await _cache_put(key, text)
    return ExplainEventResponse(explanation=text, cached=False)
