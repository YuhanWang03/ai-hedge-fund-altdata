"""Citation-integrity verification for Agent V2 answers."""

from __future__ import annotations

import json
import re

from v2.agent_v2.models import AnswerMode, EvidenceItem, ToolEnvelope, VerificationReport

_CITATION = re.compile(r"\[([A-Za-z0-9_.:-]+)\]")
_SENTENCE = re.compile(r"[^。！？!?\n]+(?:[。！？!?]+|$)(?:\s*\[[A-Za-z0-9_.:-]+\])*")
_DIRECT_CAUSE = re.compile(r"主要原因|直接原因|直接驱动|由.{0,20}推动|因为|催化剂是|归因于")
_HEDGED_CAUSE = re.compile(r"可能|或许|候选|低置信|中置信|尚未确认|无法确认|不能确认")
_INTRADAY_MARKER = re.compile(r"盘中|截至查询时|截至.{0,30}(?:ET|美东)")
_FINAL_VOLUME_CONCLUSION = re.compile(r"缩量|放量|量能不足|成交量.{0,12}(?:未|没有).{0,6}(?:放大|跟上)|持续性存疑")
_INTRADAY_CAUTION = re.compile(r"不能|无法|不应|不得|尚不能|待收盘|未收盘|尚未定型|不宜")


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
            warnings.extend(_citation_integrity_warnings(answer or "", evidence, results or [], grounding))
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


def _citation_integrity_warnings(answer: str, evidence: list[EvidenceItem], results: list[ToolEnvelope], grounding) -> list[str]:
    """Require nearby citations to support nearby figures and causal certainty."""

    known = {item.id: item for item in evidence}
    warnings: list[str] = []
    market_answer = any(result.capability.startswith("market.") for result in results)
    for raw_sentence in _SENTENCE.findall(answer):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        cited_ids = [value for value in _CITATION.findall(sentence) if value in known]
        plain = _CITATION.sub("", sentence)
        if cited_ids:
            cited_items = [known[value] for value in cited_ids]
            cited_observations = "\n".join(
                " ".join(
                    value
                    for value in (
                        item.claim,
                        str(item.value) if item.value is not None else "",
                        json.dumps(item.metadata, ensure_ascii=False, default=str),
                    )
                    if value
                )
                for item in cited_items
            )
            local = grounding.check(plain, cited_observations)
            if local.ungrounded:
                warnings.append("引用未支持邻近数字：" + ", ".join(local.ungrounded[:4]))
            if _DIRECT_CAUSE.search(plain) and not _HEDGED_CAUSE.search(plain):
                weak = [item for item in cited_items if item.metadata.get("claim_role") == "candidate_driver"]
                if weak:
                    warnings.append("候选归因被表述为已确认原因：" + ", ".join(item.id for item in weak[:2]))
            intraday_price = any(item.metadata.get("evidence_scope") == "price" and item.metadata.get("is_intraday") for item in cited_items)
            if intraday_price and not _INTRADAY_MARKER.search(plain):
                warnings.append("盘中价格被表述为完整收盘口径")
            intraday_volume = any(item.metadata.get("evidence_scope") == "volume" and item.metadata.get("is_intraday") for item in cited_items)
            if intraday_volume and _FINAL_VOLUME_CONCLUSION.search(plain) and not _INTRADAY_CAUTION.search(plain):
                warnings.append("未收盘成交量被用于判定放量、缩量或持续性")
        elif market_answer and grounding.check(plain, "").total:
            warnings.append("行情事实缺少邻近引用")
    return list(dict.fromkeys(warnings))
