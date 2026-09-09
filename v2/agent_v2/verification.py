"""Citation-integrity verification for Agent V2 answers.

The verifier is domain-neutral.  Beyond the universal checks (every citation
must exist, every figure must trace to evidence, a citation must support the
figures next to it) it enforces rules that adapters attach to their own
evidence and results:

``EvidenceItem.metadata``
    ``constraints``: list of ``{"require": regex, "warning": str}`` or
    ``{"forbid": regex, "unless": regex | None, "warning": str}``; each applies
    to the sentence that cites the item.
    ``citable``: ``False`` marks evidence the answer must not cite; the warning
    text comes from ``uncitable_warning``.

``ToolEnvelope.metadata``
    ``require_cited_numbers``: every sentence with a figure needs a citation.
    ``answer_constraints``: list of ``{"forbid": regex, "warning": str}`` for
    the whole answer, or ``{"max_cited": {"metadata": {...}, "max": n,
    "warning": str}}`` capping how many of *this result's* items with matching
    metadata may be cited.
"""

from __future__ import annotations

import json
import re
from typing import Any

from v2.agent_v2.models import AnswerMode, EvidenceItem, ToolEnvelope, VerificationReport

_CITATION = re.compile(r"\[([A-Za-z0-9_.:-]+)\]")
_SENTENCE = re.compile(r"[^。！？!?\n]+(?:[。！？!?]+|$)(?:\s*\[[A-Za-z0-9_.:-]+\])*")


def verify_answer(
    answer: str,
    evidence: list[EvidenceItem],
    *,
    answer_mode: AnswerMode,
    results: list[ToolEnvelope] | None = None,
) -> VerificationReport:
    known = {item.id for item in evidence}
    cited = set(_CITATION.findall(answer or ""))
    unknown = tuple(sorted(cited - known))
    warnings: list[str] = []
    ungrounded: tuple[str, ...] = ()
    traced: tuple[str, ...] = ()
    if answer_mode not in {AnswerMode.GENERAL_KNOWLEDGE, AnswerMode.INSUFFICIENT_EVIDENCE}:
        if evidence and not cited:
            warnings.append("answer contains evidence but cites none of it")
        if not evidence and answer:
            warnings.append("grounded answer has no structured evidence")
        if evidence:
            from v2.agent import grounding

            answer_without_citations = _CITATION.sub("", answer or "")
            observations = _observations(evidence) + "\n" + json.dumps(
                [
                    {
                        "summary": result.summary,
                        "metrics": result.metrics,
                        "findings": _without_identifiers(result.findings),
                        "limitations": result.limitations,
                    }
                    for result in results or []
                ],
                ensure_ascii=False,
                default=str,
            )
            report = grounding.check(answer_without_citations, observations)
            ungrounded = tuple(report.ungrounded)
            traced = tuple(dict.fromkeys(report.traced))
            warnings.extend(_sentence_warnings(answer or "", evidence, results or [], grounding))
            warnings.extend(_answer_warnings(answer or "", evidence, results or []))
    return VerificationReport(
        ok=not unknown and not warnings and not ungrounded,
        unknown_citations=unknown,
        ungrounded_numbers=ungrounded,
        traced_numbers=traced,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _observations(items: list[EvidenceItem]) -> str:
    return "\n".join(
        " ".join(
            value
            for value in (
                item.claim,
                str(item.value) if item.value is not None else "",
                json.dumps(item.metadata, ensure_ascii=False, default=str),
            )
            if value
        )
        for item in items
    )


def locate_number(number: str, evidence: list[EvidenceItem], *, limit: int = 3) -> list[str]:
    """Ids of the evidence items whose observations contain ``number`` verbatim."""

    needle = str(number).strip()
    if not needle:
        return []
    found = [item.id for item in evidence if needle in _observations([item])]
    return found[:limit]


def _without_identifiers(value):
    """Remove opaque IDs before numeric grounding; digits inside IDs are not facts."""

    if isinstance(value, dict):
        return {
            key: _without_identifiers(item)
            for key, item in value.items()
            if not str(key).lower().endswith(("_id", "_ids")) and str(key).lower() != "id"
        }
    if isinstance(value, list):
        return [_without_identifiers(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_identifiers(item) for item in value)
    return value


def _matches(pattern: Any, text: str) -> bool:
    return bool(pattern) and re.search(str(pattern), text) is not None


def _sentence_warnings(answer: str, evidence: list[EvidenceItem], results: list[ToolEnvelope], grounding) -> list[str]:
    """Require nearby citations to support nearby figures and honour evidence-level rules."""

    known = {item.id: item for item in evidence}
    require_cited_numbers = any(result.metadata.get("require_cited_numbers") for result in results)
    warnings: list[str] = []
    for raw_sentence in _SENTENCE.findall(answer):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        cited_items = [known[value] for value in _CITATION.findall(sentence) if value in known]
        plain = _CITATION.sub("", sentence)
        if not cited_items:
            if require_cited_numbers and grounding.check(plain, "").total:
                warnings.append("行情事实缺少邻近引用")
            continue
        local = grounding.check(plain, _observations(cited_items))
        if local.ungrounded:
            warnings.append("引用未支持邻近数字：" + ", ".join(local.ungrounded[:4]))
        for item in cited_items:
            if not item.metadata.get("citable", True):
                warnings.append(str(item.metadata.get("uncitable_warning") or f"引用了不可展示的证据：{item.id}"))
            for rule in item.metadata.get("constraints") or []:
                if not isinstance(rule, dict):
                    continue
                warning = str(rule.get("warning") or f"证据 {item.id} 的表述规则未满足")
                if rule.get("require") and not _matches(rule["require"], plain):
                    warnings.append(warning)
                if rule.get("forbid") and _matches(rule["forbid"], plain) and not _matches(rule.get("unless"), plain):
                    warnings.append(warning)
    return warnings


def _answer_warnings(answer: str, evidence: list[EvidenceItem], results: list[ToolEnvelope]) -> list[str]:
    """Apply result-level rules that look at the answer as a whole."""

    known = {item.id: item for item in evidence}
    cited = [known[value] for value in _CITATION.findall(answer) if value in known]
    warnings: list[str] = []
    for result in results:
        own = {item.id for item in result.evidence}
        for rule in result.metadata.get("answer_constraints") or []:
            if not isinstance(rule, dict):
                continue
            if rule.get("forbid") and _matches(rule["forbid"], answer):
                warnings.append(str(rule.get("warning") or f"{result.capability} 的回答规则未满足"))
            cap = rule.get("max_cited")
            if isinstance(cap, dict):
                wanted = dict(cap.get("metadata") or {})
                # A cap is about this result's evidence: six tickers may each show one candidate.
                matching = {item.id for item in cited if item.id in own and all(item.metadata.get(key) == value for key, value in wanted.items())}
                if len(matching) > int(cap.get("max", 0)):
                    warnings.append(str(cap.get("warning") or f"{result.capability} 引用了过多同类证据"))
    return warnings
