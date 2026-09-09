"""Default evidence-preserving synthesis; replaceable with an LLM synthesizer.

The synthesizer knows nothing about any one domain.  An adapter that can
render its own result better than a claim list puts the prose in
``ToolEnvelope.metadata["narrative"]`` and the synthesizer uses it verbatim.
"""

from __future__ import annotations

from v2.agent_v2.models import (
    EvidenceItem,
    ExecutionPlan,
    NormalizedRequest,
    ToolEnvelope,
)


class EvidenceSummarySynthesizer:
    """Small deterministic fallback that keeps the V2 core runnable offline."""

    supports_general_knowledge = False

    def synthesize(
        self,
        request: NormalizedRequest,
        plan: ExecutionPlan,
        results: list[ToolEnvelope],
        evidence: list[EvidenceItem],
    ) -> str:
        if not results:
            if plan.requires_confirmation:
                return "这是一个写操作。请先确认具体操作内容；当前没有执行任何修改。"
            if plan.answer_mode.value == "general_knowledge":
                return "该问题被识别为通用知识问题；尚未接入 Agent V2 的知识回答模型。"
            if plan.assumptions and plan.route.value == "command":
                return plan.assumptions[0]
            return "现有信息不足以确定需要调用的能力，请补充标的或希望查询的范围。"

        blocks: list[str] = []
        for result in results:
            narrative = str(result.metadata.get("narrative") or "").strip()
            if result.ok and narrative:
                blocks.append(narrative)
                continue
            lines: list[str] = []
            if result.summary:
                lines.append(result.summary)
            elif result.ok:
                lines.append(f"{result.capability} 已完成。")
            else:
                detail = result.errors[0] if result.errors else "未知错误"
                lines.append(f"{result.capability} 未完成：{detail}")
            for item in result.evidence[:4]:
                if not item.metadata.get("citable", True):
                    continue
                if item.claim and item.claim != result.summary:
                    lines.append(f"- {item.claim} [{item.id}]")
                elif item.claim:
                    lines[-1] += f" [{item.id}]"
            lines.extend(f"数据限制：{item}" for item in result.limitations[:3])
            blocks.append("\n".join(lines))
        return "\n\n".join(block for block in blocks if block)
