"""LLM post-release summarizer for the Earnings Agent.

Template-Fill discipline (same rule as v2/screening/narrator.py):
- Python supplies every number (EPS, revenue, surprise pct, last-4Q streak).
- LLM only fills the qualitative slots: ``bull`` / ``bear`` / ``narrative``.
- The renderer interpolates numbers into the card; the LLM never speaks them.

Behaviour on failure mirrors the narrator: log + return ``("", "", "")`` so the
cron path can still ship a numbers-only card instead of crashing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from v2.earnings.models import EarningsHistorical

logger = logging.getLogger(__name__)


# Fingerprint phrase — kept in sync with
# v2/observability/hooks.py::LLM_ROLE_FINGERPRINTS so the dashboard can
# tag this LLM call as role=earnings_summarizer.
_SYSTEM_PROMPT = (
    "你是一名财报发布总结分析师。基于刚发布的季度财报数据，给出 bull + bear 各一句**定性**判断。\n"
    "\n"
    "【关键约束 — Template Fill 模式】\n"
    "1. **严禁输出任何具体数字** — EPS、营收、百分比、年份 全部禁止\n"
    "2. 数字会由代码自动从结构化数据填入卡片，你不需要重复\n"
    "3. 可以用定性词：「显著」「明显」「超出」「不及」「连续」「持续」「逆转」「企稳」\n"
    "\n"
    "【可用上下文】\n"
    "- eps_surprise：BEAT / MISS / MEET（本季 vs 一致预期）\n"
    "- last_4q_surprises：最近 4 季 surprise 序列（最新在前）\n"
    "- eps_surprise_pct：EPS 相对预期偏差（fraction）\n"
    "- revenue_surprise_pct：营收相对预期偏差（fraction）\n"
    "- transcript_snippet：（可选）电话会要点摘录\n"
    "\n"
    "【判断重点】\n"
    "- bull：抓本季亮点 / 趋势改善（连续 BEAT、营收加速、利润率扩张）\n"
    "- bear：抓最关键风险（即便 BEAT 也可能有隐忧——指引疲软、Q/Q 放缓、超预期幅度收窄）\n"
    "- 如果是 MISS：bull 抓一线积极信号（如有），bear 抓核心问题\n"
    "- 各 ≤ 40 字\n"
    "\n"
    "【输出格式】\n"
    "只输出 JSON，不要 markdown：\n"
    '{"bull": "...", "bear": "...", "narrative": "一句话总评 ≤ 30 字"}'
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarize(
    ticker: str,
    latest: EarningsHistorical,
    recent: list[EarningsHistorical] | None = None,
    transcript_snippet: str | None = None,
) -> tuple[dict[str, str], int]:
    """Generate bull/bear/narrative for a just-released earnings report.

    Returns ``({"bull": ..., "bear": ..., "narrative": ...}, total_tokens)``.
    On any failure returns ``({}, 0)`` — caller can ship the numeric-only
    card without LLM color.
    """
    if not latest.has_quarterly_data:
        return {}, 0

    payload = _build_payload(ticker, latest, recent or [], transcript_snippet)
    try:
        return _invoke_llm(payload)
    except Exception as exc:
        logger.warning("earnings summarizer LLM call failed: %s", exc)
        return {}, 0


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_payload(
    ticker: str,
    latest: EarningsHistorical,
    recent: list[EarningsHistorical],
    transcript_snippet: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": ticker,
        "report_period": latest.report_period,
        "filing_date": latest.filing_date,
        "eps_surprise": latest.eps_surprise,
    }
    eps_pct = latest.eps_surprise_pct()
    if eps_pct is not None:
        payload["eps_surprise_pct"] = round(eps_pct * 100, 2)
    rev_pct = latest.revenue_surprise_pct()
    if rev_pct is not None:
        payload["revenue_surprise_pct"] = round(rev_pct * 100, 2)

    streak = [r.eps_surprise for r in recent if r.eps_surprise != "UNKNOWN"]
    if streak:
        payload["last_4q_surprises"] = streak[:4]

    if transcript_snippet:
        # Hard cap — prompt budget protection.
        payload["transcript_snippet"] = transcript_snippet[:600]

    return payload


def _invoke_llm(payload: dict[str, Any]) -> tuple[dict[str, str], int]:
    """Real LLM call. Isolated so tests can monkey-patch this seam."""
    # Local import — keeps module importable even where langchain isn't
    # installed (e.g. lightweight cron environments / smoke tests).
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_deepseek import ChatDeepSeek

    user_prompt = (
        f"刚发布的 {payload['ticker']} 财报数据：\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        '输出格式：{"bull": "...", "bear": "...", "narrative": "..."}'
    )

    llm = ChatDeepSeek(model="deepseek-chat", temperature=0.3)
    response = llm.invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ])

    content = _strip_code_fence(response.content or "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("earnings summarizer non-JSON: %s\nContent: %r", exc, content[:200])
        return {}, _extract_tokens(response)

    if not isinstance(parsed, dict):
        return {}, _extract_tokens(response)

    out = {
        "bull": _clean(parsed.get("bull", "")),
        "bear": _clean(parsed.get("bear", "")),
        "narrative": _clean(parsed.get("narrative", "")),
    }
    return out, _extract_tokens(response)


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # Drop the opening fence (with optional language tag) and closing fence.
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -len("```")]
    return s.strip()


def _clean(s: Any) -> str:
    return str(s).strip() if s else ""


def _extract_tokens(response: Any) -> int:
    """Best-effort token count from a langchain response. 0 if unknown."""
    meta = getattr(response, "response_metadata", None) or {}
    usage = meta.get("token_usage") or meta.get("usage") or {}
    try:
        return int(usage.get("total_tokens", 0))
    except (TypeError, ValueError):
        return 0
