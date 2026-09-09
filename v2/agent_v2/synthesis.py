"""Default evidence-preserving synthesis; replaceable with an LLM synthesizer.

The synthesizer knows nothing about any one domain.  An adapter that can
render its own result better than a claim list puts the prose in
``ToolEnvelope.metadata["narrative"]`` and the synthesizer uses it verbatim.
"""

from __future__ import annotations

import re
from typing import Any

from v2.agent_v2.models import (
    EvidenceItem,
    ExecutionPlan,
    NormalizedRequest,
    ToolEnvelope,
)
from v2.agent_v2.text import plain_text

_SUPERLATIVE = re.compile(r"最|哪只|哪个|哪几|哪些|排名|排序|前几|谁")


def ranking_lead(text: str, results: list[ToolEnvelope]) -> str:
    """Answer a superlative question directly from a result's ranked table.

    An adapter that returns a table publishes ``metadata["positions"]`` (rows)
    and ``metadata["rankable"]`` (which row field answers which wording).  The
    synthesizer knows nothing about the domain: it picks the rule whose words
    the question uses, sorts, and cites the result's evidence.
    """

    if not _SUPERLATIVE.search(text or ""):
        return ""
    for result in results:
        rows = result.metadata.get("positions")
        rules = result.metadata.get("rankable")
        if not isinstance(rows, list) or not isinstance(rules, list) or not result.evidence:
            continue
        # Rules whose topic words the question uses come first; a rule is
        # only usable when the question also names a direction for it.
        usable = [rule for rule in rules if isinstance(rule, dict)]
        candidates = [rule for rule in usable if rule.get("topic") and re.search(str(rule["topic"]), text)]
        candidates += [rule for rule in usable if not rule.get("topic")]
        chosen: dict[str, Any] | None = None
        low = high = False
        for rule in candidates:
            low = bool(re.search(str(rule.get("low") or "$^"), text))
            high = bool(re.search(str(rule.get("high") or "$^"), text))
            if low != high:
                chosen = rule
                break
        if chosen is None:
            continue
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
        return f"{lead}{citation}。"
    return ""


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

        blocks: list[str] = []
        lead = ranking_lead(request.text, results)
        if lead:
            blocks.append(lead)
        for result in results:
            narrative = str(result.metadata.get("narrative") or "").strip()
            if result.ok and narrative:
                blocks.append(narrative)
                continue
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
            blocks.append("\n".join(lines))
        return "\n\n".join(block for block in blocks if block)
