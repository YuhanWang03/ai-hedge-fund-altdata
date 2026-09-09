"""A deterministic starter planner; an LLM planner can replace this port later.

The planner reads a request as *scope* × *topics*:

* scope — the user's account (``持仓``, ``我的``), the watchlist, or explicit
  tickers;
* topics — account facts (P&L, risk, earnings calendar), user state, macro,
  a named manager's 13F, an ARK ETF, and per-ticker topics (a move
  explanation or a research focus).

Per-ticker topics on an account or watchlist scope become fan-out tasks: the
plan stays static and the holdings result decides which tickers run.
"""

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

# -- single-ticker market questions (kept ahead of composition) ---------------
_MOVE_EXPLANATION = re.compile(r"(?:为什么|原因|何故).{0,12}(?:涨|跌|异动|波动)|(?:涨|跌|异动|波动).{0,12}(?:为什么|原因|怎么回事|怎么了|何故)|(?:最近|今天|今日).{0,8}(?:怎么回事|怎么了)", re.I)
_RECENT_PERFORMANCE = re.compile(
    r"(?:最近|近期|今天|今日|本周|这周|本月|这个月|近\s*\d+\s*(?:天|日|周|月)).{0,12}(?:股价|价格|走势|表现|涨|跌|涨跌|回报|收益率)"
    r"|(?:股价|价格).{0,8}(?:走势|表现|涨跌|回报|收益率)|(?:走势|涨跌|跑赢|跑输)|(?:股票|股价|价格)?表现(?:如何|怎么样|怎样|好吗|好不好)|成交量|量比|波动率|放量|缩量",
    re.I,
)
_NON_PRICE_PERFORMANCE = re.compile(r"经营|业务|基本面|财务|财报|业绩|盈利|营收|利润|毛利|现金流|估值|投资逻辑|值不值得|技术面|技术指标|均线|RSI|CMF", re.I)

# -- commands ----------------------------------------------------------------
_WATCHLIST = re.compile(r"关注列表|关注|自选|watchlist", re.I)
_ALERT = re.compile(r"提醒|预警|alert", re.I)
_REMOVE = re.compile(r"移除|删除|取消|remove|delete|cancel", re.I)
_ABOVE = re.compile(r"涨到|涨过|涨破|突破|高于|超过|以上|above|over", re.I)
_BELOW = re.compile(r"跌到|跌过|跌破|低于|跌至|以下|below|under", re.I)
_PRICE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:美元|美金|块|元|usd|\$)?", re.I)
_ALERT_ID = re.compile(r"(?:提醒|预警|alert)\s*(?:#|编号|id)?\s*(\d+)|(?:#|编号|id)\s*(\d+)", re.I)

# -- scope -------------------------------------------------------------------
_PORTFOLIO_WORDS = re.compile(r"持仓|仓位|组合|账户|portfolio", re.I)
_SELF = re.compile(r"我(?!们)")
_WATCHLIST_SCOPE = re.compile(r"关注列表|关注了|自选|watchlist|关注的", re.I)
_LIST_RANKING = re.compile(r"哪只|哪个|哪几只|哪些|每只|每个|那几只|那些|谁|最", re.I)
#: A list question needs an evaluative word before "which one" means "look at each".
_LIST_EVALUATE = re.compile(r"最|值得|表现|怎么样|如何|强|弱|好|差|狠|危险", re.I)
_HELP = re.compile(r"你能帮我做什么|你能做什么|能做什么|有什么功能|会做什么|怎么用|如何使用", re.I)
_BRIEFING = re.compile(r"值得注意|该知道|需要注意|有什么新情况|有什么动静|需要关注的", re.I)

# -- account topics ----------------------------------------------------------
_PERFORMANCE = re.compile(r"盈亏|赚|亏|收益|补回来|回报", re.I)
_RISK = re.compile(r"风险|回撤|集中度|暴露|占多少|各占|占比|健康|分化|危险|减仓|加仓", re.I)
_POSITION_WEIGHT = re.compile(r"占仓|仓位占比|集中度|权重|占.{0,4}仓位", re.I)
_EARNINGS_SCHEDULE = re.compile(r"(?:快|即将|近期|接下来|未来|两周|下周|这周|本周|谁要|谁会|日历|离.{0,6}最近|哪些.{0,6}发财报).{0,12}财报|财报.{0,12}(?:日历|快到|临近|最近的|最近|谁)|谁要发财报|要发财报|离.{0,4}财报.{0,4}(?:最近|最快)", re.I)
_EARNINGS_EACH = re.compile(r"(?:下次|各自|每只|都是什么时候|分别).{0,12}财报|财报.{0,8}(?:都是什么时候|分别|各自)", re.I)
_POSITIONING = re.compile(r"加仓|减仓|建仓|清仓", re.I)

# -- user state --------------------------------------------------------------
_STATE_ALERTS = re.compile(r"提醒列表|有哪些提醒|哪些提醒|提醒有哪些|未触发", re.I)
_STATE_SETTINGS = re.compile(r"阈值|推送|设置", re.I)

# -- macro -------------------------------------------------------------------
_MACRO_OVERVIEW = re.compile(r"宏观|大盘|市场(?:怎么|最近|现在|环境|情绪|出什么|怎样)|VIX|美债|利率环境|好时候", re.I)
_RELEASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bCPI\b|消费者物价|通胀数据", re.I), "cpi"),
    (re.compile(r"\bPCE\b", re.I), "pce"),
    (re.compile(r"\bNFP\b|非农|就业报告", re.I), "nfp"),
    (re.compile(r"\bGDP\b", re.I), "gdp"),
    (re.compile(r"\bPPI\b|生产者物价", re.I), "ppi"),
    (re.compile(r"初请|失业金", re.I), "claims"),
    (re.compile(r"\bFOMC\b|美联储|议息|利率决议|联储", re.I), "fomc"),
)

# -- named managers and ARK ETFs ---------------------------------------------
_MANAGERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"巴菲特|buffett|伯克希尔|berkshire", re.I), "buffett"),
    (re.compile(r"burry|伯里|scion", re.I), "burry"),
    (re.compile(r"ackman|阿克曼|pershing", re.I), "ackman"),
    (re.compile(r"einhorn|greenlight", re.I), "einhorn"),
    (re.compile(r"renaissance|文艺复兴|rentech|西蒙斯", re.I), "renaissance"),
    (re.compile(r"citadel|城堡", re.I), "citadel"),
    (re.compile(r"coatue", re.I), "coatue"),
    (re.compile(r"two\s*sigma", re.I), "twosigma"),
    (re.compile(r"d\.?\s*e\.?\s*shaw", re.I), "deshaw"),
    (re.compile(r"木头姐|cathie|凯茜|\bARK\b(?!\s*ETF)", re.I), "ark"),
)
_ARK_ETF = re.compile(r"\b(ARK[KQWGFX])\b", re.I)

# -- per-ticker topics -------------------------------------------------------
_EXPLAIN_MOVE = re.compile(r"为什么(?:涨|跌|动|大涨|大跌)|涨跌原因|异动|怎么了|出什么事|出了什么事|涨了吗|跌了吗|逆势|跌得最|涨得最|最近怎么样|最近如何|怎么样了", re.I)
_FOCUSES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"内部人|高管.{0,4}(?:买|卖)|insider|卖得|在卖|在买", re.I), "ownership"),
    (re.compile(r"谁在持有|持有人|机构持股|机构持有|机构投资者|机构.{0,4}持仓|前十大", re.I), "ownership"),
    (re.compile(r"\bSEC\b|8-?K|10-?[QK]|申报|重大事项|going concern|高管离职|公告|filing", re.I), "filings"),
    (re.compile(r"财报|超预期|\bEPS\b|业绩|earnings", re.I), "earnings"),
    (re.compile(r"资金流|资金.{0,4}(?:流入|流出)|\bCMF\b|\bRSI\b|流出|流入|money\s*flow", re.I), "market"),
    (re.compile(r"产业链|供应商|客户|上下游|供应链", re.I), "supply_chain"),
    (re.compile(r"估值|市盈率|市销率|贵不贵|valuation", re.I), "valuation"),
    (re.compile(r"催化|事件|新闻", re.I), "catalysts"),
    (re.compile(r"风险|危险|risk", re.I), "risk"),
    (re.compile(r"完整|全面|深度", re.I), "full"),
)
_RESEARCH_CHANGES = re.compile(r"(?:研究|结论|观点|评级).{0,8}(?:变化|上次|之前)|相比上次|和上次|较上次", re.I)

_HELP_ANSWER = (
    "我可以：查看持仓、盈亏和组合风险；研究单只股票的财报、内部人交易、SEC 申报、资金流、估值和产业链；"
    "解释个股异动并对比行业基准；查询宏观数据、知名基金经理的 13F 和 ARK ETF 动向；"
    "对持仓或关注列表逐只排查；运行筛选、回测和事件研究；管理关注列表和价格提醒。"
)


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


def _focuses(text: str) -> list[str]:
    found: list[str] = []
    for pattern, focus in _FOCUSES:
        if pattern.search(text) and focus not in found:
            found.append(focus)
    if "full" in found or len(found) > 2:
        return ["full"]
    return found


def parse_mutation(text: str, entities: tuple[str, ...]) -> tuple[PlanTask | None, str]:
    """Map a command sentence to one ``state.mutate`` task, or explain what is missing."""

    ticker = entities[0] if entities else ""
    if _ALERT.search(text):
        if _REMOVE.search(text):
            match = _ALERT_ID.search(text)
            alert_id = next((group for group in match.groups() if group), "") if match else ""
            if not alert_id:
                return None, "取消提醒需要提醒编号（可先查看提醒列表）。"
            return PlanTask("mutation", "state.mutate", {"operation": "alert.remove", "payload": {"alert_id": int(alert_id)}}, purpose=f"取消提醒 #{alert_id}"), ""
        if not ticker:
            return None, "设置提醒需要股票代码。"
        direction = "above" if _ABOVE.search(text) else "below" if _BELOW.search(text) else ""
        prices = [float(value) for value in _PRICE.findall(text) if value and float(value) > 0]
        if not direction or not prices:
            return None, f"为 {ticker} 设置提醒需要方向（涨到/跌到）和目标价。"
        payload = {"ticker": ticker, "direction": direction, "target_price": prices[-1]}
        label = "涨到" if direction == "above" else "跌到"
        return PlanTask("mutation", "state.mutate", {"operation": "alert.add", "payload": payload}, purpose=f"当 {ticker} {label} {prices[-1]:g} 美元时提醒"), ""
    if not ticker:
        return None, "关注列表操作需要股票代码。"
    if _REMOVE.search(text):
        return PlanTask("mutation", "state.mutate", {"operation": "watchlist.remove", "payload": {"ticker": ticker}}, purpose=f"将 {ticker} 移出关注列表"), ""
    return PlanTask("mutation", "state.mutate", {"operation": "watchlist.add", "payload": {"ticker": ticker}}, purpose=f"将 {ticker} 加入关注列表"), ""


def _budget(tasks: list[PlanTask]) -> BudgetClass:
    if any(task.fan_out for task in tasks):
        return BudgetClass.PORTFOLIO
    count = len(tasks)
    if count <= 1:
        return BudgetClass.DIRECT
    if count <= 2:
        return BudgetClass.FOCUSED
    if count <= 5:
        return BudgetClass.STANDARD
    if count <= 7:
        return BudgetClass.PORTFOLIO
    return BudgetClass.DEEP


class RulePlanner:
    """Produces conservative plans that work without an LLM or API key."""

    def plan(self, request: NormalizedRequest, route: RouteDecision) -> ExecutionPlan:
        text, entities = request.text, request.entities
        if route.kind == RouteKind.GENERAL_KNOWLEDGE:
            return ExecutionPlan(objective=text, route=route.kind, answer_mode=AnswerMode.GENERAL_KNOWLEDGE, budget=BudgetClass.DIRECT)
        if route.kind == RouteKind.COMMAND:
            task, problem = parse_mutation(text, entities)
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                tasks=(task,) if task else (),
                answer_mode=AnswerMode.TOOL_GROUNDED,
                budget=BudgetClass.DIRECT,
                requires_confirmation=task is not None,
                direct_answer=problem,
                assumptions=("No mutation is executed until the user confirms the exact operation.",),
            )
        if route.kind in {RouteKind.LAB, RouteKind.ASYNC}:
            return self._lab(text, entities, route)
        if _HELP.search(text):
            return ExecutionPlan(objective=text, route=route.kind, answer_mode=AnswerMode.GENERAL_KNOWLEDGE, budget=BudgetClass.DIRECT, direct_answer=_HELP_ANSWER)

        tickers = tuple(ticker for ticker in entities if not _ARK_ETF.fullmatch(ticker))
        if len(tickers) == 1 and _MOVE_EXPLANATION.search(text):
            tasks = [PlanTask("market-move", "market.explain_move", {"ticker": tickers[0]}, purpose="separate confirmed market facts from candidate move drivers")]
            for index, focus in enumerate(_focuses(text), 1):
                tasks.append(PlanTask(f"research-{index}", "research.stock", {"ticker": tickers[0], "focus": focus}, purpose=f"collect {focus} evidence"))
            return ExecutionPlan(objective=text, route=route.kind, tasks=tuple(tasks[:5]), answer_mode=AnswerMode.RESEARCH_GROUNDED, budget=_budget(tasks[:5]), web_fallback_allowed=request.allow_web)
        if len(tickers) == 1 and _RECENT_PERFORMANCE.search(text) and not _NON_PRICE_PERFORMANCE.search(text):
            return ExecutionPlan(
                objective=text,
                route=route.kind,
                tasks=(PlanTask("market-performance", "market.performance", {"ticker": tickers[0]}, purpose="measure recent returns, volume and benchmark-relative performance"),),
                answer_mode=AnswerMode.TOOL_GROUNDED,
                budget=BudgetClass.FOCUSED,
            )
        if len(tickers) == 1 and _RESEARCH_CHANGES.search(text):
            return ExecutionPlan(objective=text, route=route.kind, tasks=(PlanTask("research-change", "research.changes", {"ticker": tickers[0]}, purpose="compare stored research snapshots"),), answer_mode=AnswerMode.RESEARCH_GROUNDED, budget=BudgetClass.DIRECT)

        tasks = self._compose(text, tickers)
        grounded = any(task.capability.startswith(("research.", "market.")) for task in tasks)
        return ExecutionPlan(
            objective=text,
            route=route.kind,
            tasks=tuple(tasks),
            answer_mode=AnswerMode.RESEARCH_GROUNDED if grounded else AnswerMode.TOOL_GROUNDED,
            budget=_budget(tasks),
            web_fallback_allowed=request.allow_web,
        )

    # -- pieces ---------------------------------------------------------------

    @staticmethod
    def _lab(text: str, entities: tuple[str, ...], route: RouteDecision) -> ExecutionPlan:
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

    def _compose(self, text: str, tickers: tuple[str, ...]) -> list[PlanTask]:
        tasks: list[PlanTask] = []
        ids: set[str] = set()

        def add(task_id: str, capability: str, arguments: dict | None = None, *, purpose: str = "", depends_on: tuple[str, ...] = (), fan_out: dict | None = None) -> None:
            if task_id in ids:
                return
            ids.add(task_id)
            tasks.append(PlanTask(task_id, capability, dict(arguments or {}), depends_on=depends_on, purpose=purpose, fan_out=fan_out))

        managers = [key for pattern, key in _MANAGERS if pattern.search(text)]
        ark_etfs = list(dict.fromkeys(match.upper() for match in _ARK_ETF.findall(text)))
        watchlist_scope = bool(_WATCHLIST_SCOPE.search(text))
        explicit_portfolio = bool(_PORTFOLIO_WORDS.search(text)) and not (managers or ark_etfs) or bool(_PORTFOLIO_WORDS.search(text) and _SELF.search(text))
        account_scope = explicit_portfolio or (bool(_SELF.search(text)) and not tickers and not watchlist_scope)

        # account-level topics
        if _PERFORMANCE.search(text) and (account_scope or not tickers) and not managers:
            periods = [period for pattern, period in ((r"周", "week"), (r"月", "month"), (r"今天|当日|今日|日内", "day")) if re.search(pattern, text)] or ["day"]
            for period in periods:
                add(f"account-performance-{period}", "account.performance", {"period": period}, purpose=f"account P&L for the {period}")
        if _RISK.search(text) and (account_scope or not tickers) and not managers:
            add("account-risk", "account.risk", purpose="collect portfolio-level risk")
        if _POSITION_WEIGHT.search(text) and not managers:
            add("account-risk", "account.risk", purpose="position weights and concentration")
        if _EARNINGS_SCHEDULE.search(text) and not tickers:
            add("account-earnings", "account.earnings_schedule", {"days": 14}, purpose="upcoming earnings across holdings and watchlist")
        if _BRIEFING.search(text) and not tickers:
            add("account-portfolio", "account.portfolio", purpose="identify positions and weights")
            add("account-risk", "account.risk", purpose="collect portfolio-level risk")
            add("account-earnings", "account.earnings_schedule", {"days": 14}, purpose="upcoming earnings across holdings and watchlist")
        if _POSITIONING.search(text) and not tickers:
            add("macro-overview", "macro.overview", purpose="market backdrop before changing exposure")
            add("account-risk", "account.risk", purpose="collect portfolio-level risk")
            if account_scope:
                add("account-portfolio", "account.portfolio", purpose="identify positions and weights")

        # user state
        if _STATE_ALERTS.search(text) or (_ALERT.search(text) and not _STATE_SETTINGS.search(text)):
            add("state-alerts", "state.read", {"section": "alerts"}, purpose="read configured alerts")
        elif _STATE_SETTINGS.search(text):
            add("state-settings", "state.read", {"section": "settings"}, purpose="read user settings")
        if watchlist_scope:
            add("state-watchlist", "state.read", {"section": "watchlist"}, purpose="read the watchlist")

        # macro, managers, ARK
        if _MACRO_OVERVIEW.search(text):
            add("macro-overview", "macro.overview", purpose="macro dashboard")
        for pattern, release in _RELEASES:
            if pattern.search(text):
                add(f"macro-{release}", "macro.release", {"release_type": release}, purpose=f"latest {release.upper()} release")
        for manager in managers[:2]:
            add(f"manager-{manager}", "institutional.manager_portfolio", {"manager": manager}, purpose=f"latest 13F for {manager}")
        for symbol in ark_etfs[:2]:
            add(f"etf-{symbol}", "etf.ark_activity", {"symbol": symbol}, purpose=f"{symbol} holdings and activity")

        # per-ticker topics
        explain = bool(_EXPLAIN_MOVE.search(text))
        focuses = _focuses(text)
        list_scope = account_scope or watchlist_scope
        if tickers:
            if not explain and not focuses:
                focuses = [_focus(text)]
            if len(tickers) >= 2 and focuses and not explain:
                add("research-compare", "research.compare", {"tickers": list(tickers[:4]), "dimensions": focuses[:2]}, purpose="compare identical dimensions")
            else:
                for ticker in tickers[:4]:
                    if explain:
                        add(f"move-{ticker}", "market.explain_move", {"ticker": ticker}, purpose=f"explain {ticker}'s move")
                    for focus in focuses[:2]:
                        add(f"research-{ticker}-{focus}", "research.stock", {"ticker": ticker, "focus": focus}, purpose=f"{focus} research for {ticker}")
        elif list_scope:
            # Account-level wording ("组合风险", "快发财报") is answered by the
            # account capabilities; per-ticker fan-out needs a per-ticker ask.
            if "risk" in focuses and not _LIST_RANKING.search(text):
                focuses = [focus for focus in focuses if focus != "risk"]
            if "earnings" in focuses and _EARNINGS_SCHEDULE.search(text) and not _EARNINGS_EACH.search(text):
                focuses = [focus for focus in focuses if focus != "earnings"]
            per_ticker = explain or bool(focuses) or bool(_EARNINGS_EACH.search(text))
            if _EARNINGS_EACH.search(text) and "earnings" not in focuses:
                focuses = [*focuses, "earnings"]
            only_scope = all(task.capability in {"account.portfolio", "state.read"} for task in tasks)
            if not per_ticker and _LIST_RANKING.search(text) and _LIST_EVALUATE.search(text) and only_scope:
                explain, per_ticker = True, True
            source = "state-watchlist" if watchlist_scope and not explicit_portfolio else "account-portfolio"
            if source == "account-portfolio" and (explicit_portfolio or per_ticker or not tasks):
                add("account-portfolio", "account.portfolio", purpose="identify positions and weights")
            if per_ticker:
                if explain:
                    add("move-each", "market.explain_move", {}, purpose="explain each holding's recent move", depends_on=(source,), fan_out={"from": source, "field": "tickers", "argument": "ticker", "max": 8})
                for focus in focuses[:2]:
                    add(f"research-each-{focus}", "research.stock", {"focus": focus}, purpose=f"{focus} research for each holding", depends_on=(source,), fan_out={"from": source, "field": "tickers", "argument": "ticker", "max": 8})
        return tasks[:7]
