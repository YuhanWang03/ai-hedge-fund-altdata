"""Citation-integrity verification for Agent V2 answers."""

from __future__ import annotations

import json
import re

from v2.agent_v2.models import AnswerMode, EvidenceItem, VerificationReport

_CITATION = re.compile(r"\[([A-Za-z0-9_.:-]+)\]")


def verify_answer(
    answer: str,
    evidence: list[EvidenceItem],
    *,
    answer_mode: AnswerMode,
) -> VerificationReport:
    known = {item.id for item in evidence}
    cited = set(_CITATION.findall(answer or ""))
    unknown = tuple(sorted(cited - known))
    warnings: list[str] = []
    ungrounded: tuple[str, ...] = ()
    if answer_mode not in {AnswerMode.GENERAL_KNOWLEDGE, AnswerMode.INSUFFICIENT_EVIDENCE}:
        if evidence and not cited:
            warnings.append("answer contains evidence but cites none of it")
        if not evidence and answer:
            warnings.append("grounded answer has no structured evidence")
        if evidence:
            from v2.agent import grounding

            answer_without_citations = _CITATION.sub("", answer or "")
            observations = "\n".join(
                " ".join(
                    value
                    for value in (
                        item.claim,
                        str(item.value) if item.value is not None else "",
                        json.dumps(item.metadata, ensure_ascii=False, default=str),
                    )
                    if value
                )
                for item in evidence
            )
            ungrounded = tuple(grounding.check(answer_without_citations, observations).ungrounded)
    return VerificationReport(
        ok=not unknown and not warnings and not ungrounded,
        unknown_citations=unknown,
        ungrounded_numbers=ungrounded,
        warnings=tuple(warnings),
    )
