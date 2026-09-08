"""Default evidence-preserving synthesis; replaceable with an LLM synthesizer."""

from __future__ import annotations

from v2.agent_v2.models import (
    EvidenceItem,
    ExecutionPlan,
    NormalizedRequest,
    ToolEnvelope,
)


def _pct(value: float | None) -> str:
    return "数据不足" if value is None else f"{float(value):+.2%}"


def _cite(item: EvidenceItem | None) -> str:
    return f"[{item.id}]" if item is not None else ""


def _scoped(evidence: list[EvidenceItem], scope: str) -> list[EvidenceItem]:
    return [item for item in evidence if item.metadata.get("evidence_scope") == scope]


def _driver_text(item: EvidenceItem) -> str:
    text = str(item.metadata.get("driver_text") or "").strip()
    if text:
        return text.rstrip("。")
    text = item.claim.split("：", 1)[-1].split("；校验备注", 1)[0]
    return text.rstrip("。")


def _displayable_candidate(item: EvidenceItem) -> bool:
    note = str(item.metadata.get("note") or "")
    rejected = ("无直接证据", "未提及", "关联弱", "不匹配", "Tier 3", "长期预测")
    return float(item.confidence or 0) >= 0.5 and not any(marker in note for marker in rejected)


def synthesize_market_answer(results: list[ToolEnvelope], evidence: list[EvidenceItem]) -> str | None:
    """Return a compact, deterministic answer for evidence-sensitive market queries."""

    market = next((result for result in results if result.capability in {"market.performance", "market.explain_move"}), None)
    if market is None or not market.ok:
        return None
    ticker = market.subject
    price = next(iter(_scoped(evidence, "price")), None)
    volume = next(iter(_scoped(evidence, "volume")), None)
    benchmarks = _scoped(evidence, "benchmark")
    if market.capability == "market.performance":
        returns_item = next(iter(_scoped(evidence, "returns")), None)
        volatility = next(iter(_scoped(evidence, "volatility")), None)
        returns = market.metrics.get("returns") or {}
        close = market.metrics.get("close")
        day = returns.get("1d")
        direction = "上涨" if day is not None and day > 0 else "下跌" if day is not None and day < 0 else "基本持平"
        trend = "偏强" if (returns.get("5d") or 0) > 0 and (returns.get("1m") or 0) > 0 else "偏弱" if (returns.get("5d") or 0) < 0 and (returns.get("1m") or 0) < 0 else "分化"
        if market.metrics.get("is_intraday") and price is not None:
            first = f"{ticker} 最近的股价表现{trend}。{price.claim.rstrip('。')}；近 5 日回报 {_pct(returns.get('5d'))}，近 1 月回报 {_pct(returns.get('1m'))}{_cite(price)}{_cite(returns_item)}。"
        else:
            first = (
                f"{ticker} 最近的股价表现{trend}。截至 {market.as_of}，收盘价为 {float(close):.2f} 美元，"
                f"最新交易日{direction} {abs(float(day)):.2%}；近 5 日回报 {_pct(returns.get('5d'))}，近 1 月回报 {_pct(returns.get('1m'))}"
                f"{_cite(price)}{_cite(returns_item)}。"
            )
        relative = market.metrics.get("relative_returns") or {}
        relative_parts: list[str] = []
        for item in benchmarks:
            benchmark = str(item.metadata.get("benchmark") or "基准")
            values = relative.get(benchmark) or {}
            relative_parts.append(
                f"相对 {benchmark}，单日超额 {_pct(values.get('1d'))}，近 5 日 {_pct(values.get('5d'))}，近 1 月 {_pct(values.get('1m'))}{_cite(item)}"
            )
        second = "；".join(relative_parts) + "。" if relative_parts else "行业与大盘基准数据暂时不足。"
        volume_ratio = market.metrics.get("volume_ratio")
        if market.metrics.get("is_intraday") and volume is not None:
            volume_text = f"{volume.claim.rstrip('。')}{_cite(volume)}。"
        else:
            volume_text = (
                f"当日成交量约 {int(market.metrics.get('volume') or 0) / 10_000:.0f} 万股，是 30 日平均水平的 {float(volume_ratio):.2f} 倍{_cite(volume)}。"
                if volume is not None and volume_ratio is not None
                else "成交量对比数据暂时不足。"
            )
        volatility_value = market.metrics.get("annualized_volatility_21d")
        risk_text = (
            f"近 21 个交易日年化波动率约为 {float(volatility_value):.2%}{_cite(volatility)}，说明短线波动仍然较大。"
            if volatility is not None and volatility_value is not None
            else ""
        )
        next_step = "待收盘后再判断量能，并观察相对行业的超额表现能否延续。" if market.metrics.get("is_intraday") else "接下来重点观察成交量能否跟上，以及相对行业的超额表现能否延续。"
        last = f"{volume_text}{risk_text}{next_step}"
        return "\n\n".join((first, second, last))

    change = market.metrics.get("price_change_pct")
    close = market.metrics.get("price")
    direction = "上涨" if change is not None and change > 0 else "下跌" if change is not None and change < 0 else "基本持平"
    certainty = "确实" if change else ""
    first_parts = [f"{price.claim.rstrip('。')}{_cite(price)}。"] if price is not None else [f"{ticker} 在 {market.as_of}{certainty}{direction} {abs(float(change)):.2%}，价格为 {float(close):.2f} 美元。"]
    if volume is not None:
        first_parts.append(f"{volume.claim.rstrip('。')}{_cite(volume)}。")
    if benchmarks:
        first_parts.append(f"{benchmarks[0].claim.rstrip('。')}{_cite(benchmarks[0])}。")
    assessment = next(iter(_scoped(evidence, "attribution")), None)
    confirmed = _scoped(evidence, "driver")
    candidates = _scoped(evidence, "candidate")
    if confirmed:
        reason_text = "；".join(f"{_driver_text(item)}{_cite(item)}" for item in confirmed[:2])
        second = f"目前能直接支持的高置信度驱动是：{reason_text}。"
    else:
        second = f"但“为什么{direction}”目前还不能下定论：暂未找到可核实的同日催化剂，具体触发原因尚未确认{_cite(assessment)}。"
    displayable = [item for item in candidates if _displayable_candidate(item)]
    if displayable:
        candidate = max(displayable, key=lambda item: float(item.confidence or 0))
        note = str(candidate.metadata.get("note") or "").strip().rstrip("。")
        note_text = f"；但{note}" if note else ""
        second += f"目前最相关的一条候选线索是“{_driver_text(candidate)}”{note_text}，因此它仍只能作为排查方向{_cite(candidate)}。"
    if market.metrics.get("is_intraday"):
        third = f"从盘面看，股价明显跑赢行业基准{_cite(benchmarks[0] if benchmarks else None)}。但当前成交量仍是盘中累计值{_cite(volume)}，不能用它推断放量、缩量或上涨持续性；应待收盘后再判断量能，并等待公司公告或可核验的同日新闻确认催化剂。"
    else:
        third = f"从盘面看，股价明显跑赢行业基准{_cite(benchmarks[0] if benchmarks else None)}，但成交量未同比例放大{_cite(volume)}，因此不宜单凭涨幅追认某个原因。接下来应观察放量延续性，并等待公司公告或可核验的同日新闻确认催化剂。"
    return "\n\n".join(("".join(first_parts), second, third))


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
        market_answer = synthesize_market_answer(results, evidence)
        if market_answer is not None:
            return market_answer
        if not results:
            if plan.requires_confirmation:
                return "这是一个写操作。请先确认具体操作内容；当前没有执行任何修改。"
            if plan.answer_mode.value == "general_knowledge":
                return "该问题被识别为通用知识问题；尚未接入 Agent V2 的知识回答模型。"
            return "现有信息不足以确定需要调用的能力，请补充标的或希望查询的范围。"

        lines: list[str] = []
        for result in results:
            if result.summary:
                lines.append(result.summary)
            elif result.ok:
                lines.append(f"{result.capability} 已完成。")
            else:
                detail = result.errors[0] if result.errors else "未知错误"
                lines.append(f"{result.capability} 未完成：{detail}")
            for item in result.evidence[:4]:
                if item.claim and item.claim != result.summary:
                    lines.append(f"- {item.claim} [{item.id}]")
                elif item.claim:
                    lines[-1] += f" [{item.id}]"
            lines.extend(f"数据限制：{item}" for item in result.limitations[:3])
        return "\n".join(lines)
