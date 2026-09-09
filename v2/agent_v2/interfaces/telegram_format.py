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


@dataclass(frozen=True)
class SourceEntry:
    number: int
    label: str
    url: str = ""


def source_entries(ids: tuple[str, ...], evidence: list[EvidenceItem], *, limit: int = 12) -> list[SourceEntry]:
    """One entry per cited item: its number, where it came from plus its claim, and a link if any.

    Labels are unescaped; the transport turns them into HTML.
    """

    by_id = {item.id: item for item in evidence}
    entries: list[SourceEntry] = []
    for index, identifier in enumerate(ids[:limit], start=1):
        item = by_id.get(identifier)
        if item is None:
            continue
        origin = item.source_title or item.source_id or str(item.metadata.get("evidence_scope") or "")
        claim = (item.claim or "").strip()
        if len(claim) > 80:
            claim = claim[:77].rstrip() + "…"
        label = f"{origin} · {claim}" if origin and claim else origin or claim or identifier
        entries.append(SourceEntry(index, label, link_for(item)))
    return entries


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


def web_label(*, requested: bool, enabled: bool) -> str:
    """The 网页 field of the header, and the hint that tells the user how to change it."""

    if not enabled:
        return "未启用（服务端 AGENT_V2_WEB_ENABLED 未开）"
    if not requested:
        return "已关闭（去掉 --noweb 可用新闻归因）"
    return "已启用"
