"""Citation-integrity verification for Agent V2 answers."""

from __future__ import annotations

import json
import re

from v2.agent_v2.models import AnswerMode, EvidenceItem, ToolEnvelope, VerificationReport

_CITATION = re.compile(r"\[([A-Za-z0-9_.:-]+)\]")


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
    if answer_mode not in {AnswerMode.GENERAL_KNOWLEDGE, AnswerMode.INSUFFICIENT_EVIDENCE}:
        if evidence and not cited:
            warnings.append("answer contains evidence but cites none of it")
        if not evidence and answer:
            warnings.append("grounded answer has no structured evidence")
        if evidence:
            from v2.agent import grounding

            answer_without_citations = _CITATION.sub("", answer or "")
            evidence_observations = "\n".join(
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
            result_observations = json.dumps(
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
            observations = f"{evidence_observations}\n{result_observations}"
            ungrounded = tuple(grounding.check(answer_without_citations, observations).ungrounded)
    return VerificationReport(
        ok=not unknown and not warnings and not ungrounded,
        unknown_citations=unknown,
        ungrounded_numbers=ungrounded,
        warnings=tuple(warnings),
    )


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
