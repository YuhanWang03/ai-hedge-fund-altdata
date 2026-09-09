"""Shape an Agent V2 result for a phone screen, without python-telegram-bot.

The web frontends show the evidence ids, the synthesis badge and the
verification badge as UI; a Telegram message has to carry the same
information in its text.  Everything here is plain string work so the bot
tests can run where the Telegram library cannot be imported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from v2.agent_v2.models import AgentResult, EvidenceItem

_CITATION = re.compile(r"\[([A-Za-z0-9_.:~-]+)\]")

#: How the answer text came to be, in the words the web badge uses.
SYNTHESIS_LABELS = {
    "clean": "模型回答",
    "repaired": "模型回答（修正一轮）",
    "knowledge": "模型回答",
    "fallback": "兜底摘要",
    "deterministic": "规则摘要",
}


@dataclass(frozen=True)
class NumberedAnswer:
    text: str
    #: Evidence ids in order of first appearance; ``[n]`` in the text is ``ids[n - 1]``.
    ids: tuple[str, ...]


def number_citations(answer: str, evidence: list[EvidenceItem]) -> NumberedAnswer:
    """Replace ``[evidence-id]`` citations with ``[1]``, ``[2]``… by first appearance.

    Only ids that exist in the evidence list are renumbered, so a bracketed
    ticker or figure the model wrote stays as it is.  A run of adjacent
    citations such as ``[a][b]`` becomes ``[1][2]``.
    """

    known = {item.id for item in evidence}
    order: list[str] = []

    def replace(match: re.Match[str]) -> str:
        identifier = match.group(1)
        if identifier not in known:
            return match.group(0)
        if identifier not in order:
            order.append(identifier)
        return f"[{order.index(identifier) + 1}]"

    return NumberedAnswer(_CITATION.sub(replace, answer or ""), tuple(order))


def compact_attributions(answer: str, result: AgentResult) -> str:
    """Swap each worst-day attribution block for its one-paragraph form.

    The deterministic fallback pastes the attributor's narrative verbatim, so
    the substitution is exact; a model-written answer never contains the
    narrative and is left alone.
    """

    text = answer or ""
    for envelope in result.results:
        if envelope.capability != "market.attribute_move":
            continue
        full = str(envelope.metadata.get("narrative") or "").strip()
        compact = str(envelope.metadata.get("narrative_compact") or "").strip()
        if full and compact and full in text:
            text = text.replace(full, compact)
    return text


#: Where an evidence item came from, in the reader's language.
SOURCE_LABELS = {
    "market_data": "日线行情",
    "anomaly_memory": "盯盘记忆",
    "sec_edgar": "SEC 申报",
    "web_news": "网页新闻",
    "web_search": "网页搜索",
    "move_attribution": "归因判断",
    "research_engine": "研究引擎",
    "research_store": "研究库",
    "legacy_responder": "账户卡片",
}
_LEGACY_TITLE = "Existing deterministic responder"


def _origin(item: EvidenceItem) -> str:
    if item.source_id in SOURCE_LABELS:
        return SOURCE_LABELS[item.source_id]
    if item.source_title == _LEGACY_TITLE:
        return SOURCE_LABELS["legacy_responder"]
    return _one_line(item.source_title or item.source_id or str(item.metadata.get("evidence_scope") or "")) or "其他来源"


def _one_line(text: str, limit: int = 80) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _ranges(numbers: list[int]) -> str:
    """``[2, 3, 4, 7, 9, 10]`` → ``2–4、7、9–10``."""

    parts: list[str] = []
    for number in sorted(set(numbers)):
        if parts and parts[-1][1] == number - 1:
            parts[-1][1] = number
        else:
            parts.append([number, number])
    return "、".join(str(low) if low == high else f"{low}–{high}" for low, high in parts)


@dataclass(frozen=True)
class SourceEntry:
    #: ``"3"`` for one item, ``"2–8、12"`` for a group of items from one origin.
    numbers: str
    label: str
    url: str = ""


def source_entries(ids: tuple[str, ...], evidence: list[EvidenceItem], *, max_linked: int = 8) -> list[SourceEntry]:
    """The 来源 list: linked items one per line, the rest grouped by origin.

    Every cited number appears exactly once.  Filings and news carry a
    title and a link, so each gets its own line; the many price and memory
    items behind a drawdown answer are one line per origin, because the
    answer already quotes their claims.
    """

    by_id = {item.id: item for item in evidence}
    linked: list[SourceEntry] = []
    grouped: dict[str, list[int]] = {}
    overflow: list[int] = []
    for index, identifier in enumerate(ids, start=1):
        item = by_id.get(identifier)
        if item is None:
            continue
        url = link_for(item)
        if url and len(linked) < max_linked:
            title = _one_line(item.source_title) or _origin(item)
            # A filing's title already says what its claim says.
            claim = "" if item.source_id == "sec_edgar" else _one_line(item.claim, 60)
            label = title if not claim or claim.startswith(title) else f"{title} · {claim}"
            linked.append(SourceEntry(str(index), label, url))
        elif url:
            overflow.append(index)
        else:
            grouped.setdefault(_origin(item), []).append(index)
    entries = [SourceEntry(_ranges(numbers), origin) for origin, numbers in grouped.items()]
    if overflow:
        entries.append(SourceEntry(_ranges(overflow), "其余带链接的来源（略）"))
    return sorted(linked + entries, key=lambda entry: int(re.match(r"\d+", entry.numbers).group(0)))


def link_for(item: EvidenceItem) -> str:
    url = item.source_url or ""
    return url if urlparse(url).scheme in {"http", "https"} else ""


def synthesis_label(result: AgentResult) -> str:
    outcome = str((result.synthesis or {}).get("outcome") or "")
    return SYNTHESIS_LABELS.get(outcome, "")


def verification_label(result: AgentResult) -> str:
    report = result.verification
    if report.ok and not report.warnings:
        return "通过"
    problems = len(report.unknown_citations) + len(report.ungrounded_numbers) + len(report.warnings)
    return f"有警告（{problems}）" if problems else "通过"


def warning_line(result: AgentResult) -> str:
    """The verifier's first complaint about the delivered answer, or empty."""

    report = result.verification
    problems = [*report.warnings]
    if report.unknown_citations:
        problems.append("未知引用：" + "、".join(report.unknown_citations[:3]))
    if report.ungrounded_numbers:
        problems.append("未落地数字：" + "、".join(report.ungrounded_numbers[:3]))
    return _one_line(problems[0], 120) if problems else ""


def _attempt_clauses(synthesis: dict) -> list[str]:
    """One clause per rejected draft: stage, then the verifier's first complaint."""

    stages = {"draft": "初稿", "repair": "修正稿", "error": "模型调用"}
    clauses: list[str] = []
    for attempt in synthesis.get("attempts") or []:
        if not isinstance(attempt, dict) or attempt.get("ok"):
            continue
        problems = [*(attempt.get("warnings") or [])]
        if attempt.get("unknown_citations"):
            problems.append("未知引用 " + "、".join(str(value) for value in attempt["unknown_citations"][:2]))
        if attempt.get("ungrounded_numbers"):
            problems.append("未落地数字 " + "、".join(str(value) for value in attempt["ungrounded_numbers"][:2]))
        stage = stages.get(str(attempt.get("stage")), str(attempt.get("stage") or "草稿"))
        clauses.append(f"{stage}：{_one_line(problems[0], 70) if problems else '校验未通过'}")
    return clauses


def fallback_reason(result: AgentResult) -> str:
    """Why the model's drafts were rejected, one clause per attempt, or empty when not a fallback."""

    synthesis = result.synthesis or {}
    if synthesis.get("outcome") != "fallback":
        return ""
    clauses = _attempt_clauses(synthesis)
    return "；".join(clauses[:2]) if clauses else "模型草稿未通过校验"


def repair_reason(result: AgentResult) -> str:
    """What the first draft failed on when the repaired draft was delivered, or empty."""

    synthesis = result.synthesis or {}
    if synthesis.get("outcome") != "repaired":
        return ""
    clauses = _attempt_clauses(synthesis)
    return clauses[0] if clauses else ""


def completion_note(result: AgentResult) -> str:
    """How many citations the code completed on the model's drafts, or empty."""

    completions = (result.synthesis or {}).get("citation_completions") or []
    return f"引用补全 {len(completions)} 处" if completions else ""


def web_label(*, requested: bool, enabled: bool) -> str:
    """The 网页 field of the header, and the hint that tells the user how to change it."""

    if not enabled:
        return "未启用（服务端 AGENT_V2_WEB_ENABLED 未开）"
    if not requested:
        return "已关闭（去掉 --noweb 可用新闻归因）"
    return "已启用"
