"""Default evidence-preserving synthesis; replaceable with an LLM synthesizer.

The synthesizer knows nothing about any one domain.  An adapter that can
render its own result better than a claim list puts the prose in
``ToolEnvelope.metadata["narrative"]`` and the synthesizer uses it verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from v2.agent_v2.models import (
    EvidenceItem,
    ExecutionPlan,
    NormalizedRequest,
    ToolEnvelope,
)
from v2.agent_v2.text import plain_text

_SUPERLATIVE = re.compile(r"最|哪只|哪个|哪几|哪些|排名|排序|前几|谁")


@dataclass
class RankingLead:
    text: str = ""
    entities: tuple[str, ...] = ()
    result: ToolEnvelope | None = field(default=None, repr=False)

    def __bool__(self) -> bool:
        return bool(self.text)


def choose_rankable(text: str, rules: Any) -> tuple[dict[str, Any], bool, bool] | None:
    """The rankable rule the wording selects, with its direction (low, high)."""

    if not isinstance(rules, list):
        return None
    usable = [rule for rule in rules if isinstance(rule, dict)]
    candidates = [rule for rule in usable if rule.get("topic") and re.search(str(rule["topic"]), text)]
    candidates += [rule for rule in usable if not rule.get("topic")]
    for rule in candidates:
        low = bool(re.search(str(rule.get("low") or "$^"), text))
        high = bool(re.search(str(rule.get("high") or "$^"), text))
        if low != high:
            return rule, low, high
    return None


def position_row(results: list[ToolEnvelope], ticker: str) -> tuple[ToolEnvelope, dict[str, Any]] | None:
    """The result and row describing ``ticker`` in a position table, if any result carries one."""

    for result in results:
        rows = result.metadata.get("positions")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and str(row.get("ticker") or "").upper() == ticker.upper():
                return result, row
    return None


def frame_lead(frame: dict[str, Any], results: list[ToolEnvelope]) -> str:
    """Open a framed follow-up by restating what it refers to, from this run's evidence.

    A question like "为什么跌这么多" after a P/L ranking is about the loss
    since purchase.  The restatement cites the freshly fetched position row,
    and when today's move points the other way it says so in one clause
    rather than letting the day's figure answer a question about months.
    """

    ticker = str(frame.get("ticker") or "")
    found = position_row(results, ticker)
    if not ticker or found is None:
        return ""
    source, row = found
    key, text_key = str(frame.get("field") or "pl_pct"), str(frame.get("text") or "pl_pct_text")
    value_text = row.get(text_key) or row.get(key)
    citation = f"[{source.evidence[0].id}]" if source.evidence else ""
    entry = row.get("avg_entry_price")
    entry_text = f"，成本价 ${float(entry):.2f}" if isinstance(entry, (int, float)) else ""
    lines = [f"你问的是 {ticker} {frame.get('label') or '这一项'}：{value_text}{entry_text}{citation}。"]
    value = row.get(key)
    for result in results:
        if result.subject.upper() != ticker.upper() or not result.ok:
            continue
        returns = result.metrics.get("returns") if isinstance(result.metrics.get("returns"), dict) else {}
        today = returns.get("1d", result.metrics.get("price_change_pct"))
        if not isinstance(today, (int, float)) or not isinstance(value, (int, float)):
            continue
        candidates: list[str] = []
        if (today > 0) != (value > 0) and today != 0:
            price = next((item for item in result.evidence if item.metadata.get("evidence_scope") == "price"), None)
            direction = "上涨" if today > 0 else "下跌"
            # An intraday price carries a wording rule: the sentence citing
            # it must say it is intraday, or the verifier rejects it.
            intraday = price is not None and (price.metadata.get("is_intraday") or any(isinstance(rule, dict) and rule.get("require") for rule in price.metadata.get("constraints") or []))
            when = "今日盘中" if intraday else "今日"
            candidates.append(f"{when}为{direction}（{float(today):+.2%}），与{frame.get('label') or '上述区间'}是不同区间{f'[{price.id}]' if price else ''}。")
        candidates.extend(decline_timing(value, returns, result))
        # Sentences the code writes go through the same verifier as the
        # model's; one that fails is dropped rather than shipped.
        lines.extend(sentence for sentence in candidates if _self_checks(sentence, result))
        break
    return "\n".join(lines)


def _self_checks(sentence: str, result: ToolEnvelope) -> bool:
    from v2.agent_v2.models import AnswerMode
    from v2.agent_v2.verification import verify_answer

    return verify_answer(sentence, list(result.evidence), answer_mode=AnswerMode.TOOL_GROUNDED, results=[result]).ok


_WINDOW_LABELS = (("5d", "近 5 日"), ("1m", "近 1 月"), ("3m", "近 3 月"), ("1y", "近 1 年"))


def decline_timing(total_pct: Any, returns: dict[str, Any], result: ToolEnvelope) -> list[str]:
    """Where in time a loss since purchase sits, read off the return windows.

    Each window's return is compared with the loss: the first window that
    accounts for at least half of it is where the decline mostly happened;
    when none does, the decline predates the longest window.  A positive
    longest window while the position loses means the purchase came after
    the run-up, which is said as well.
    """

    if not isinstance(total_pct, (int, float)) or total_pct >= 0:
        return []
    total = float(total_pct) / 100.0
    windows = [(key, label, float(returns[key])) for key, label in _WINDOW_LABELS if isinstance(returns.get(key), (int, float))]
    if not windows:
        return []
    item = next((item for item in result.evidence if item.metadata.get("evidence_scope") == "returns"), None)
    citation = f"[{item.id}]" if item is not None else ""
    described = "、".join(f"{label} {value:+.2%}" for _, label, value in windows)
    sentences: list[str] = []
    for _, label, value in windows:
        if value < 0 and value / total >= 0.5:
            sentences.append(f"对照区间回报（{described}），这段跌幅大部分落在{label}内{citation}。")
            break
    else:
        _, longest_label, _ = windows[-1]
        sentences.append(f"对照区间回报（{described}），这段跌幅主要发生在{longest_label}以前{citation}。")
    _, longest_label, longest_value = windows[-1]
    if longest_value > 0:
        sentences.append(f"{longest_label} {longest_value:+.2%} 而该持仓仍在浮亏，说明买入点在这轮上涨之后的高位{citation}。")
    return sentences


_DATED = re.compile(r"\d{4}-\d{2}-\d{2}|\d{4}\s*年\s*\d{1,2}\s*月|\d{1,2}\s*月\s*\d{1,2}\s*日|\b(?:Q[1-4]|FY)\s?\d{2,4}\b")


def benchmark_line(result: ToolEnvelope) -> str:
    """Only the benchmark-relative sentence of a performance narrative; the returns were used above."""

    narrative = plain_text(str(result.metadata.get("narrative") or ""))
    for line in narrative.split("\n"):
        if "相对" in line and "[" in line:
            return line.strip()
    return ""


def catalyst_lines(result: ToolEnvelope, since: str = "") -> str:
    """A research or history result in a framed answer: its dated, citable findings, not a thesis.

    ``since`` (ISO date) keeps only events inside the decline window when
    the item carries a date in its metadata or ``as_of``.
    """

    lines: list[str] = []
    for item in result.evidence:
        if not item.metadata.get("citable", True) or item.metadata.get("citation_kind") in {"metrics", "limitations"}:
            continue
        claim = plain_text(item.claim)
        # A catalyst is an event: it has a date.  Undated fundamentals and
        # valuation figures are not what "why did it fall" asks for.
        if not claim or not _DATED.search(claim):
            continue
        day = str(item.metadata.get("date") or item.as_of or "")[:10]
        if since and day and day < since:
            continue
        lines.append(f"- {claim} [{item.id}]")
        if len(lines) >= 6:
            break
    if not lines:
        lines.append(f"{result.subject} 期间未查到可核对的催化剂（财报、公告或新闻）。")
    limitation_item = next((item for item in result.evidence if item.metadata.get("citation_kind") == "limitations"), None)
    suffix = f" [{limitation_item.id}]" if limitation_item is not None else ""
    lines.extend(f"数据限制：{item}{suffix}" for item in result.limitations[:2])
    if not result.ok:
        detail = result.errors[0] if result.errors else "未知错误"
        lines.append(f"{result.capability} 未完成：{detail}")
    return "\n".join(lines)


def ranking_lead(text: str, results: list[ToolEnvelope]) -> RankingLead:
    """Answer a superlative question directly from a result's ranked table.

    An adapter that returns a table publishes ``metadata["positions"]`` (rows)
    and ``metadata["rankable"]`` (which row field answers which wording).  The
    synthesizer knows nothing about the domain: it picks the rule whose words
    the question uses, sorts, and cites the result's evidence.
    """

    if not _SUPERLATIVE.search(text or ""):
        return RankingLead()
    for result in results:
        rows = result.metadata.get("positions")
        rules = result.metadata.get("rankable")
        if not isinstance(rows, list) or not isinstance(rules, list) or not result.evidence:
            continue
        choice = choose_rankable(text, rules)
        if choice is None:
            continue
        chosen, low, high = choice
        key, text_key = str(chosen.get("field") or ""), str(chosen.get("text") or "")
        ranked = [row for row in rows if isinstance(row, dict) and isinstance(row.get(key), (int, float))]
        if not ranked:
            continue
        ranked.sort(key=lambda row: float(row[key]), reverse=high)
        citation = f"[{result.evidence[0].id}]"

        def label(row: dict[str, Any]) -> str:
            value = row.get(text_key) if text_key else row.get(key)
            return f"{row.get('ticker', '')}（{value}）"

        head, rest = ranked[0], ranked[1:3]
        direction = "最低" if low else "最高"
        lead = f"按{chosen.get('label') or key}排序，{direction}的是 {label(head)}"
        if rest:
            lead += "，其次是 " + "、".join(label(row) for row in rest)
        entities = tuple(str(row.get("ticker", "")) for row in (head, *rest) if row.get("ticker"))
        return RankingLead(f"{lead}{citation}。", entities, result)
    return RankingLead()


def ranked_answer(lead: RankingLead, results: list[ToolEnvelope]) -> str:
    """A ranking answer: the conclusion, one line per named entity, and what was not covered.

    Everything else the run fetched stays in the evidence list; the reader
    asked which one, not for a tour of every holding.
    """

    lines = [lead.text]
    for entity in lead.entities:
        for result in results:
            if result is lead.result or result.subject.upper() != entity.upper():
                continue
            if not result.ok:
                detail = result.errors[0] if result.errors else "未知错误"
                lines.append(f"{entity} {result.capability} 未完成：{detail}")
                continue
            narrative = plain_text(str(result.metadata.get("narrative") or "").strip() or result.summary)
            first = narrative.split("\n")[0].strip()
            if first and "[" not in first and result.evidence:
                first += f" [{result.evidence[0].id}]"
            if first:
                lines.append(first)
    for result in results:
        coverage = result.metadata.get("fan_out_coverage")
        if not isinstance(coverage, dict):
            continue
        uncovered = [str(value) for value in coverage.get("uncovered") or []]
        citation = f" [{result.evidence[0].id}]" if result.evidence else ""
        if uncovered:
            lines.append(f"{result.capability} 未覆盖：{'、'.join(uncovered)}{citation}。")
    return "\n".join(lines)


class EvidenceSummarySynthesizer:
    """Small deterministic fallback that keeps the V2 core runnable offline."""

    supports_general_knowledge = False

    def diagnostics(self) -> dict[str, Any]:
        return {"outcome": "deterministic", "draft": "", "attempts": []}

    def synthesize(
        self,
        request: NormalizedRequest,
        plan: ExecutionPlan,
        results: list[ToolEnvelope],
        evidence: list[EvidenceItem],
    ) -> str:
        if not results:
            if plan.direct_answer:
                return plan.direct_answer
            if plan.requires_confirmation:
                return "这是一个写操作。请先确认具体操作内容；当前没有执行任何修改。"
            if plan.answer_mode.value == "general_knowledge":
                return "该问题被识别为通用知识问题；尚未接入 Agent V2 的知识回答模型。"
            return "现有信息不足以确定需要调用的能力，请补充标的或希望查询的范围。"

        frame = request.metadata.get("context_frame")
        if isinstance(frame, dict) and frame.get("kind") == "position":
            opening = frame_lead(frame, results)
            if opening:
                found = position_row(results, str(frame.get("ticker") or ""))
                source = found[0] if found else None
                drawdown = next((result for result in results if result.capability == "market.drawdown" and result.ok), None)
                since = str(drawdown.metrics.get("window_start") or "") if drawdown is not None else ""
                blocks = [opening]
                for result in results:
                    if result is source:
                        continue
                    if result.capability in {"research.stock", "research.compare", "filings.recent", "market.anomaly_history", "web.research"}:
                        blocks.append(catalyst_lines(result, since))
                    elif result.capability == "market.performance" and result.ok:
                        blocks.append(benchmark_line(result))
                    else:
                        blocks.append(self._render(result))
                return "\n\n".join(block for block in blocks if block)
        lead = ranking_lead(request.text, results)
        if lead:
            # The ranking answer leads; results about the ranked objects
            # themselves stay in the evidence list, anything else the question
            # also asked for (P&L, risk, macro) is still rendered below.
            source = lead.result
            ranked_subjects = {str(value).upper() for value in ((source.metadata.get("tickers") if source is not None else None) or [])}
            blocks = [ranked_answer(lead, results)]
            others = [
                result
                for result in results
                if result is not source and result.subject.upper() not in ranked_subjects and not isinstance(result.metadata.get("fan_out_coverage"), dict)
            ]
            blocks.extend(self._render(result) for result in others)
            return "\n\n".join(block for block in blocks if block)
        return "\n\n".join(block for block in (self._render(result) for result in results) if block)

    @staticmethod
    def _render(result: ToolEnvelope) -> str:
        narrative = str(result.metadata.get("narrative") or "").strip()
        if result.ok and narrative:
            return narrative
        lines: list[str] = []
        only_limitations = bool(result.evidence) and all(item.metadata.get("citation_kind") == "limitations" for item in result.evidence)
        if result.summary:
            lines.append(plain_text(result.summary))
        elif not result.ok:
            detail = result.errors[0] if result.errors else "未知错误"
            lines.append(f"{result.capability} 未完成：{detail}")
        elif not only_limitations:
            lines.append(f"{result.capability} 已完成。")
        # A limitations-only result (a fan-out coverage note) is rendered
        # by its limitation line below, which already cites the item.
        for item in [] if only_limitations else result.evidence[:4]:
            if not item.metadata.get("citable", True):
                continue
            if item.claim and item.claim != result.summary:
                lines.append(f"- {plain_text(item.claim)} [{item.id}]")
            elif item.claim and lines:
                lines[-1] += f" [{item.id}]"
        # Limitations often carry figures; cite the adapter's limitation
        # evidence when it exists so the line stays verifiable.
        limitation_item = next((item for item in result.evidence if item.metadata.get("citation_kind") == "limitations"), None)
        suffix = f" [{limitation_item.id}]" if limitation_item is not None else ""
        lines.extend(f"数据限制：{item}{suffix}" for item in result.limitations[:3])
        return "\n".join(lines)
