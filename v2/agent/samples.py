"""Labelled routing cases — the seed of the evaluation set.

Each case carries the query, the intent the bot's classifier produces for it,
and the path it *should* take. That is enough to score the router without
spending a single LLM call, which means it can run in CI on every change to the
signal table.

Labelling rules used here, so the numbers mean something:

* ``single_hop`` when one existing responder already answers the question in
  full. Ranking or comparing the contents of one card still counts as single-hop
  if the card itself contains the ranking.
* ``agent`` when the answer requires facts from more than one tool, or when the
  classifier cannot place the query at all.
* Intents are what the classifier actually returns, not what would be ideal —
  including the observed case where "我持仓里哪只最危险" classifies as
  ``risk_view``, a card that never names an individual position.

Cases expected to be hard are kept in deliberately (see ``note``). A sample set
that only contains easy cases reports a flattering number and tunes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoutingCase:
    query: str
    intent: str
    expected: str          # "single_hop" | "agent" | "slash"
    ticker: str = ""
    note: str = ""


CASES: tuple[RoutingCase, ...] = (
    # -- single-ticker lookups: must stay on the fast path --------------------
    RoutingCase("NVDA 为什么涨", "explain_move", "single_hop", "NVDA"),
    RoutingCase("分析一下 AAPL", "summary", "single_hop", "AAPL"),
    RoutingCase("TSLA 什么时候发财报", "earnings_view", "single_hop", "TSLA"),
    RoutingCase("MSFT 资金流怎么样", "moneyflow_view", "single_hop", "MSFT"),
    RoutingCase("NVDA 内部人交易", "insider_view", "single_hop", "NVDA"),
    RoutingCase("AMD 最近有什么 8-K", "eight_k_view", "single_hop", "AMD",
                "「最近」是时间词不是比较级，必须被排除"),
    RoutingCase("NVDA 最新消息", "summary", "single_hop", "NVDA",
                "「最新」同上"),
    RoutingCase("特斯拉最近怎么回事", "explain_move", "single_hop", "TSLA",
                "含「怎么回事」因果词，但主体单一，快路径够用"),

    # -- whole-set lookups where one card is the whole answer -----------------
    RoutingCase("我关注了哪些股票", "watchlist_view", "single_hop", "",
                "枚举类 intent 豁免：watchlist_view 一跳返回整个集合"),
    RoutingCase("我的持仓", "portfolio_view", "single_hop", ""),
    RoutingCase("我的当日盈亏", "pnl_view", "single_hop", ""),
    RoutingCase("组合风险怎么样", "risk_view", "single_hop", "",
                "risk_view 卡本身就是组合层全景"),
    RoutingCase("宏观怎么样", "macro_view", "single_hop", ""),
    RoutingCase("最近 CPI", "release_check", "single_hop", "",
                "「最近」排除后无信号命中"),
    RoutingCase("下周谁要发财报", "earnings_calendar", "single_hop", "",
                "「谁」不在比较级表里，earnings_calendar 一跳可答"),
    RoutingCase("巴菲特最新持仓", "thirteen_f", "single_hop", ""),

    # -- ranking / comparison -------------------------------------------------
    RoutingCase("我持仓里哪只最危险", "risk_view", "agent", "",
                "实测分类为 risk_view，但该卡不排序个股"),
    RoutingCase("我持仓里哪只跌得最多", "portfolio_view", "agent", ""),
    RoutingCase("AAPL 和 MSFT 谁的财报更好", "earnings_view", "agent", "AAPL"),
    RoutingCase("NVDA 和 AMD 对比一下", "summary", "agent", "NVDA"),
    RoutingCase("我的持仓里哪些风险最大，为什么", "risk_view", "agent", ""),
    RoutingCase("watchlist 里哪只最值得关注", "watchlist_view", "agent", "",
                "枚举 intent + 比较级 → 比较级信号优先，必须进 agent"),

    # -- collection needing enumeration then per-item lookups -----------------
    RoutingCase("我持仓里有哪些快发财报了", "earnings_calendar", "agent", ""),
    RoutingCase("我关注的股票里有没有内部人在卖", "insider_view", "agent", ""),
    RoutingCase("我的持仓里有没有踩雷的", "risk_view", "agent", ""),

    # -- portfolio-level causal ----------------------------------------------
    RoutingCase("我这周为什么亏钱", "pnl_period", "agent", ""),
    RoutingCase("组合最近为什么回撤这么大", "risk_view", "agent", ""),

    # -- compound -------------------------------------------------------------
    RoutingCase("AAPL 财报怎么样，另外内部人有没有在卖", "earnings_view", "agent", "AAPL"),
    RoutingCase("NVDA 涨了吗？要不要加仓？", "explain_move", "agent", "NVDA"),

    # -- classifier dead ends -------------------------------------------------
    RoutingCase("今天有什么值得注意的", "unknown", "agent", ""),
    RoutingCase("帮我看看要不要减仓", "unknown", "agent", ""),
    RoutingCase("市场怎么了", "unknown", "agent", ""),
    RoutingCase("你能干什么", "unknown", "agent", "",
                "元问题，agent 至少能给出可用工具清单，好过「没听懂」"),

    # -- slash commands -------------------------------------------------------
    RoutingCase("/risk", "risk_view", "slash", ""),
    RoutingCase("/flow NVDA", "moneyflow_view", "slash", "NVDA"),
    RoutingCase("/ask 我持仓里哪只最危险", "unknown", "agent", "",
                "显式转义，任何 mode 下都进 agent"),
)


#: Multi-turn cases for pronoun resolution: (previous query, follow-up, expected antecedent).
PRONOUN_CASES: tuple[tuple[str, str, str], ...] = (
    ("NVDA 怎么样", "那它财报呢", "NVDA"),
    ("分析一下 CRWD", "它内部人有在卖吗", "CRWD"),
    ("AMD 为什么跌", "这只还能拿吗", "AMD"),
    ("我的持仓", "它怎么样", ""),          # 无先行词，不改写
    ("NVDA 怎么样", "AAPL 呢", "" ),      # 已含 ticker，不改写
)


# ---------------------------------------------------------------------------
# B1 — anomaly evidence top-up
# ---------------------------------------------------------------------------


@dataclass
class SimpleReason:
    """Stand-in for v2.monitoring.models.ScoredReason without the pydantic import."""

    text: str
    confidence: str = "中"
    note: str = ""


@dataclass
class SimpleAnomaly:
    """Structural stand-in for v2.monitoring.models.Anomaly.

    Only the fields the top-up reads. Keeping this dependency-free is what lets
    the whole B1 path be tested and scored in a bare environment.
    """

    ticker: str
    price_change_pct: float
    volume_ratio: float
    flags: list[str] = field(default_factory=list)
    contrarian: bool = False
    reasons: list = field(default_factory=list)


#: Scenarios covering every branch: already explained (must be skipped), all-低,
#: no reasons at all, and more unexplained items than the per-run cap allows.
ANOMALY_CASES: tuple[SimpleAnomaly, ...] = (
    SimpleAnomaly("NVDA", 0.0385, 1.8, ["volume_spike"], False,
                  [SimpleReason("与两家主权 AI 基金签署供货框架", "高")]),
    SimpleAnomaly("MSFT", 0.0121, 1.2, ["volume_spike"], False,
                  [SimpleReason("大盘普涨带动", "中")]),
    SimpleAnomaly("ARM", 0.0742, 3.4, ["volume_spike", "52w_high"], True, []),
    SimpleAnomaly("PLTR", -0.0518, 2.9, ["volume_spike"], True,
                  [SimpleReason("板块回调", "低"), SimpleReason("市场传闻", "低")]),
    SimpleAnomaly("SMCI", -0.0540, 3.1, ["volume_spike", "52w_low"], True,
                  [SimpleReason("渠道调研称订单递延", "低")]),
    SimpleAnomaly("AMD", -0.0132, 1.1, [], False,
                  [SimpleReason("跟随半导体板块", "低")]),
)
