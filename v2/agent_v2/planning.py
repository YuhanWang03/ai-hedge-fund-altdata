"""A deterministic starter planner; an LLM planner can replace this port later."""

from __future__ import annotations

import re

from v2.agent_v2.models import (
    AnswerMode,
    BudgetClass,
    ExecutionPlan,
    NormalizedRequest,
    PlanTask,
    RouteDecision,
    RouteKind,
)

_MOVE_EXPLANATION = re.compile(r"(?:为什么|原因|何故).{0,12}(?:涨|跌|异动|波动)|(?:涨|跌|异动|波动).{0,12}(?:为什么|原因|怎么回事|怎么了|何故)|(?:最近|今天|今日).{0,8}(?:怎么回事|怎么了)", re.I)
_RECENT_PERFORMANCE = re.compile(
    r"(?:最近|近期|今天|今日|本周|这周|本月|这个月|近\s*\d+\s*(?:天|日|周|月)).{0,12}(?:股价|价格|走势|表现|涨|跌|涨跌|回报|收益率)"
    r"|(?:股价|价格).{0,8}(?:走势|表现|涨跌|回报|收益率)|(?:走势|涨跌|跑赢|跑输)|(?:股票|股价|价格)?表现(?:如何|怎么样|怎样|好吗|好不好)",
    re.I,
)
_NON_PRICE_PERFORMANCE = re.compile(r"经营|业务|基本面|财务|财报|业绩|盈利|营收|利润|毛利|现金流|估值|投资逻辑|值不值得|技术面|技术指标|均线|RSI|CMF", re.I)


def _focus(text: str) -> str:
    checks = (
        (r"估值|市盈率|市销率", "valuation"),
        (r"财报|业绩|预期", "earnings"),
        (r"技术|趋势|资金流|CMF|RSI", "market"),
        (r"机构|内部人|持有人", "ownership"),
        (r"催化|事件|新闻", "catalysts"),
        (r"SEC|公告|申报|文件|8-K|10-Q|10-K", "filings"),
        (r"产业链|供应商|客户|上下游", "supply_chain"),
        (r"风险", "risk"),
        (r"完整|全面|深度", "full"),
    )
    return next((focus for pattern, focus in checks if re.search(pattern, text, re.I)), "overview")


class RulePlanner:
    """Produces conservative plans that work without an LLM or API key."""

    def plan(self, request: NormalizedRequest, route: RouteDecision) -> ExecutionPlan:
        text, entities = request.text, request.entities
        if route.kind == RouteKind.GENERAL_KNOWLEDGE:
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                answer_mode=AnswerMode.GENERAL_KNOWLEDGE,
                budget=BudgetClass.DIRECT,
            )
        if route.kind == RouteKind.COMMAND:
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                answer_mode=AnswerMode.TOOL_GROUNDED,
                budget=BudgetClass.DIRECT,
                requires_confirmation=True,
                assumptions=("No mutation is executed until the user confirms an exact operation.",),
            )
        if route.kind in {RouteKind.LAB, RouteKind.ASYNC}:
            capability = "lab.screen"
            if re.search(r"参数扫描", text):
                capability = "lab.sweep"
            elif re.search(r"事件研究|异常收益", text):
                capability = "lab.event_study"
            elif re.search(r"委员会|大师投票", text):
                capability = "lab.committee"
            elif re.search(r"回测", text):
                capability = "lab.backtest"
            args: dict = {"tickers": list(entities)} if entities else {}
            if capability == "lab.backtest":
                strategy = "committee" if "委员会" in text else "insider" if "内部人" in text else "pead" if re.search(r"PEAD|财报", text, re.I) else "momentum"
                args["strategy"] = strategy
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                tasks=(PlanTask("lab-1", capability, args, purpose="requested quantitative experiment"),),
                answer_mode=AnswerMode.TOOL_GROUNDED,
                budget=BudgetClass.DEEP if route.kind == RouteKind.ASYNC else BudgetClass.LAB,
                assumptions=("Missing experiment parameters must be disclosed before execution.",),
            )

        if len(entities) == 1 and _MOVE_EXPLANATION.search(text):
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                tasks=(PlanTask("market-move", "market.explain_move", {"ticker": entities[0]}, purpose="separate confirmed market facts from candidate move drivers"),),
                answer_mode=AnswerMode.RESEARCH_GROUNDED,
                budget=BudgetClass.FOCUSED,
                web_fallback_allowed=request.allow_web,
                stop_conditions=("price move and benchmark acquired", "causal evidence exhausted"),
            )
        if len(entities) == 1 and _RECENT_PERFORMANCE.search(text) and not _NON_PRICE_PERFORMANCE.search(text):
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                tasks=(PlanTask("market-performance", "market.performance", {"ticker": entities[0]}, purpose="measure recent returns, volume and benchmark-relative performance"),),
                answer_mode=AnswerMode.TOOL_GROUNDED,
                budget=BudgetClass.FOCUSED,
                stop_conditions=("recent price and benchmark windows acquired",),
            )

        tasks: list[PlanTask] = []
        budget = BudgetClass.DIRECT
        if route.kind == RouteKind.RESEARCH:
            budget = BudgetClass.STANDARD
            if re.search(r"持仓|组合|账户", text):
                tasks.extend(
                    [
                        PlanTask("account-portfolio", "account.portfolio", purpose="identify positions and weights"),
                        PlanTask("account-risk", "account.risk", purpose="collect portfolio-level risk"),
                    ]
                )
                budget = BudgetClass.PORTFOLIO
            elif re.search(r"变化|上次|之前", text) and len(entities) == 1:
                tasks.append(PlanTask("research-change", "research.changes", {"ticker": entities[0]}, purpose="compare stored research snapshots"))
            elif len(entities) >= 2:
                tasks.append(PlanTask("research-compare", "research.compare", {"tickers": list(entities[:4]), "dimensions": [_focus(text)]}, purpose="compare identical dimensions"))
                budget = BudgetClass.COMPARISON
            elif len(entities) == 1:
                tasks.append(PlanTask("research-stock", "research.stock", {"ticker": entities[0], "focus": _focus(text)}, purpose="collect evidence-backed stock research"))
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                tasks=tuple(tasks),
                answer_mode=AnswerMode.RESEARCH_GROUNDED,
                budget=budget,
                web_fallback_allowed=request.allow_web,
                stop_conditions=("required evidence acquired", "budget exhausted", "providers unavailable"),
            )

        # Fast lookup mapping. An empty plan is intentional: the synthesizer can
        # state that the request needs clarification instead of guessing.
        if re.search(r"持仓|仓位", text):
            tasks.append(PlanTask("lookup-1", "account.portfolio"))
        elif re.search(r"盈亏|赚|亏|收益", text) and (
            not entities or re.search(r"我的|持仓|组合|账户", text)
        ):
            period = "month" if "月" in text else "week" if "周" in text else "day"
            tasks.append(PlanTask("lookup-1", "account.performance", {"period": period}))
        elif re.search(r"组合风险|集中度|行业暴露", text):
            tasks.append(PlanTask("lookup-1", "account.risk"))
        elif re.search(r"关注列表|提醒|设置", text):
            section = "alerts" if "提醒" in text else "settings" if "设置" in text else "watchlist"
            tasks.append(PlanTask("lookup-1", "state.read", {"section": section}))
        elif len(entities) == 1:
            tasks.append(PlanTask("lookup-1", "research.stock", {"ticker": entities[0], "focus": _focus(text)}))
            budget = BudgetClass.FOCUSED
        return ExecutionPlan(
            objective=text,
            route=route.kind,
            tasks=tuple(tasks),
            answer_mode=AnswerMode.TOOL_GROUNDED,
            budget=budget,
            web_fallback_allowed=request.allow_web,
        )
