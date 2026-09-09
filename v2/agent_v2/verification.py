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
    the whole answer, ``{"max_cited": {"metadata": {...}, "max": n,
    "warning": str}}`` capping how many of *this result's* items with matching
    metadata may be cited, or ``{"require_cited": {"metadata": {...},
    "warning": str}}`` demanding that at least one of them is.
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


_DIGITS = re.compile(r"\d")


def _significant_digits(token: str) -> int:
    digits = _DIGITS.findall(str(token))
    return len("".join(digits).lstrip("0"))


def complete_citations(answer: str, evidence: list[EvidenceItem], results: list[ToolEnvelope] | None = None) -> tuple[str, list[str]]:
    """Add the one citation a sentence is missing when the evidence leaves no doubt.

    A model that rewrites a paragraph tends to move a citation one sentence
    over; the figures are right, the id next to them is not.  For each
    sentence whose cited items do not carry one of its figures, when that
    figure has at least three significant digits and exactly one citable
    item in this run's evidence carries it, that item's id is appended to
    the sentence.  Existing citations stay; vaguer figures (a year, "3") and
    figures several items carry are left for the model's repair round.

    Returns the completed text and one note per completion.
    """

    if not answer or not evidence:
        return answer or "", []
    from v2.agent import grounding

    known = {item.id: item for item in evidence}
    candidates = [item for item in evidence if item.metadata.get("citable", True)]
    notes: list[str] = []
    pieces: list[str] = []
    cursor = 0
    for match in _SENTENCE.finditer(answer):
        raw = match.group(0)
        cited = [known[value] for value in _CITATION.findall(raw) if value in known]
        if not cited:
            continue
        plain = _CITATION.sub("", raw)
        local = grounding.check(plain, _observations(cited))
        if not local.ungrounded:
            continue
        additions: list[str] = []
        for token in dict.fromkeys(local.ungrounded):
            if _significant_digits(token) < 3:
                additions = []
                break
            carriers = [item for item in candidates if item not in cited and not grounding.check(token, _observations([item])).ungrounded]
            if len(carriers) != 1:
                additions = []
                break
            if carriers[0].id not in additions:
                additions.append(carriers[0].id)
                notes.append(f"{token} → [{carriers[0].id}]")
        if not additions:
            continue
        # Verify the completed sentence grounds, then splice it in.
        completed_items = cited + [known[value] for value in additions]
        if grounding.check(plain, _observations(completed_items)).ungrounded:
            continue
        # Splice the ids in before the sentence's closing punctuation, next
        # to the citation that was there.
        stripped = raw.rstrip()
        trailing = raw[len(stripped):]
        body = stripped.rstrip("。！？!?")
        closing = stripped[len(body):]
        pieces.append(answer[cursor : match.start()])
        pieces.append(body + "".join(f"[{value}]" for value in additions) + closing + trailing)
        cursor = match.end()
    if not pieces:
        return answer, []
    pieces.append(answer[cursor:])
    return "".join(pieces), notes


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
            need = rule.get("require_cited")
            if isinstance(need, dict):
                # A floor: at least one of this result's items with the given
                # metadata must be cited (the sector comparison of a drawdown).
                wanted = dict(need.get("metadata") or {})
                available = [item for item in result.evidence if all(item.metadata.get(key) == value for key, value in wanted.items())]
                if available and not any(item.id in {value.id for value in cited} for item in available):
                    warnings.append(str(need.get("warning") or f"{result.capability} 的关键证据未被引用：" + "、".join(f"[{item.id}]" for item in available[:3])))
    return warnings
