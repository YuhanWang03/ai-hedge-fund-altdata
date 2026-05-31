"""Product-manager-friendly Chinese explanations for trace events.

Each entry describes one of the dashboard's events in five short fields:
- source: where the data came from (API / DB / in-memory object)
- how:    how the call was made (HTTP / SQL / Python set op / LLM)
- what:   what we got back (rough schema)
- store:  whether (and where) we persist it
- next:   where this data goes from here

Lookups are deliberately defensive — events that don't match any key just
get no explanation field, and the frontend skips the disclosure widget.

To extend coverage to new event shapes, add an entry here. No v2/ source
changes needed.
"""

from __future__ import annotations

from typing import Any, Optional


Explanation = dict[str, str]


# ---------------------------------------------------------------------------
# The static catalogue. Keys vary by event_type — see lookup() for shape.
# ---------------------------------------------------------------------------

# api_call events keyed by (provider, endpoint_suffix)
_API: dict[tuple[str, str], Explanation] = {
    ("edgar", "Company.get_filings"): {
        "source": "SEC EDGAR 公开数据库（sec.gov）",
        "how":    "edgartools 库发 HTTP GET，带 User-Agent 头（SEC fair-use 强制要求）",
        "what":   "该 manager 所有 13F-HR 申报的清单（accession / 申报日 / 报告期）",
        "store":  "edgartools 自带磁盘缓存在 ~/.edgar/",
        "next":   "取最近 N 个 accession，逐个调 Filing.obj() 下载详细持仓表",
    },
    ("edgar", "Filing.obj"): {
        "source": "SEC EDGAR 单个 13F-HR 申报文件",
        "how":    "下载并解析该申报的 information table（XML 格式）",
        "what":   "持仓表原始行：CUSIP / ticker / 发行人 / 股数 / 市值 / 期权类型",
        "store":  "不持久化",
        "next":   "送入 _aggregate_by_cusip() 合并同一证券的多子公司报告",
    },
    ("edgar", "Filing.xbrl"): {
        "source": "SEC EDGAR 同一申报的 XBRL 财务标签",
        "how":    "edgartools 解析 XBRL 段，比纯文本 HTML 更结构化",
        "what":   "结构化财务字段（部分 13F 文件不带 XBRL，会返回 None）",
        "store":  "不持久化",
        "next":   "仅在需要结构化补充时调用，13F 主路径用不到",
    },
    ("fd", "get_prices"): {
        "source": "financialdatasets.ai 商业接口的价格序列",
        "how":    "HTTP GET 带订阅 API key；先查本地 SQLite 缓存，6 小时内的命中直接返回",
        "what":   "日级别 OHLCV：开盘 / 最高 / 最低 / 收盘 / 成交量",
        "store":  "缓存命中读 fd_cache.db；未命中则把这次结果写回缓存",
        "next":   "送入异动检测器算 30 日均量、52 周高低、板块相对强度",
    },
    ("fd", "get_financial_metrics"): {
        "source": "financialdatasets.ai 的基本面指标接口",
        "how":    "HTTP GET 带 API key；24 小时 TTL 的本地缓存",
        "what":   "市值 / 营收增速 / 毛利率 / 净利率 / ROE / 波动率",
        "store":  "缓存命中读 fd_cache.db；未命中则写回",
        "next":   "用于硬规则筛选 + 给 LLM 拼模板叙述时注入真实数字",
    },
    ("fd", "get_insider_trades"): {
        "source": "financialdatasets.ai 内部人交易接口（基于 SEC Form 4）",
        "how":    "HTTP GET；24 小时 TTL 缓存",
        "what":   "高管 / 董事的开放市场买卖记录：日期 / 股数 / 价格 / 角色",
        "store":  "缓存命中读 fd_cache.db；未命中则写回",
        "next":   "送入异动检测器找内部人簇买卖信号",
    },
    ("fd", "get_earnings"): {
        "source": "financialdatasets.ai 财报快照",
        "how":    "HTTP GET；24 小时 TTL 缓存",
        "what":   "最近季度 EPS / 营收 + 分析师预期 + surprise 百分比",
        "store":  "缓存命中读 fd_cache.db；未命中则写回",
        "next":   "/summary 卡片打 surprise tag；筛选器加「业绩超预期」标签",
    },
    ("fd", "get_company_facts"): {
        "source": "financialdatasets.ai 公司元数据",
        "how":    "HTTP GET；7 天 TTL（变化非常少）",
        "what":   "公司名 / 行业 / 子行业 / 主营业务描述 / 总部位置",
        "store":  "缓存命中读 fd_cache.db；未命中则写回",
        "next":   "供新闻实体过滤识别公司名 token，并在卡片上展示",
    },
    ("tavily", "search"): {
        "source": "Tavily 搜索引擎（专为 AI 优化的新闻 API）",
        "how":    "HTTP POST 带 API key + 关键词；故意不缓存（新闻必须实时）",
        "what":   "新闻条目列表：标题 / URL / 摘要 / 发布时间 / 来源域名",
        "store":  "不持久化（每次都跑实时搜索）",
        "next":   "先进实体过滤丢掉无关结果，再由 Verifier LLM 按来源打 Tier 评分",
    },
}


# transform events keyed by op
_TRANSFORM: dict[str, Explanation] = {
    "cusip_aggregate": {
        "source": "上一步 EDGAR Filing.obj 返回的原始持仓列表",
        "how":    "Python 字典按 CUSIP 分组，把同一证券的多行合并成一行",
        "what":   "去重后的持仓列表（同 CUSIP 一行，shares 和市值求和）",
        "store":  "scheduler 路径写 edgar.db；bot /13f 路径只读不写",
        "next":   "送入 detect_changes() 跟前一季度对比，识别新进/加仓/减仓/清仓",
    },
    "detect_changes": {
        "source": "当前季度 + 上一季度的去重后持仓列表",
        "how":    "Python 集合运算找差集；按市值变动百分比过滤显著变动",
        "what":   "PositionChange 列表，每个含 ticker / 动作 / 当前市值 / 历史市值",
        "store":  "不持久化（每次查询重算）",
        "next":   "前 20 笔送 DeepSeek 让 LLM 写一句话解读",
    },
    "entity_filter": {
        "source": "上一步 Tavily 搜索返回的新闻列表",
        "how":    "Python 子串匹配：要求标题或正文同时出现 ticker 和公司名 token",
        "what":   "通过实体校验的新闻 + 被剔除的「噪声」计数",
        "store":  "不持久化",
        "next":   "通过实体校验的新闻送 Verifier LLM 打来源 Tier 评分",
    },
    "source_tier_score": {
        "source": "实体过滤后的新闻列表",
        "how":    "按来源域名查 Tier 表：SEC/Reuters/Bloomberg=1，MarketWatch/SeekingAlpha=2，其余=3",
        "what":   "每条新闻附 Tier 标签 + 总体 Tier 分布",
        "store":  "不持久化",
        "next":   "Tier 3 整体丢弃；Tier 1+2 送 Generator LLM 做归因合成",
    },
}


# llm_call events — we identify the role from prompt content fingerprints,
# because the event itself only carries `model` and the prompt text.
_LLM_BY_ROLE: dict[str, Explanation] = {
    "intent_classifier": {
        "source": "用户输入文本",
        "how":    "DeepSeek-chat temperature=0 + 15 个 intent 枚举的 system prompt",
        "what":   "JSON：意图名 + ticker / manager / etf / 价格 / 方向参数",
        "store":  "不持久化（每次重新分类）",
        "next":   "按 intent 路由到对应 responder 函数",
    },
    "interpret_changes": {
        "source": "detect_changes() 输出的 top 20 笔变动",
        "how":    "DeepSeek-chat temperature=0.3 + 严禁编造数字的 system prompt",
        "what":   "JSON：每个 ticker 一句 ≤30 字的策略性解读",
        "store":  "不持久化",
        "next":   "解读字符串塞回 PositionChange.interpretation，渲染时附在 💡 行",
    },
    "verifier": {
        "source": "上一步过滤后的新闻 + 已带 Tier 标签",
        "how":    "DeepSeek-chat 严格 prompt：严苛评估每条理由的因果链强度",
        "what":   "每条候选理由的得分 + 通过/淘汰判定",
        "store":  "不持久化",
        "next":   "通过验证的理由送 Generator 拼成最终归因卡片",
    },
    "generator": {
        "source": "通过 Verifier 校验的 Tier 1+2 新闻片段",
        "how":    "DeepSeek-chat temperature=0 + 「数字只能来自输入」的硬约束",
        "what":   "2–3 条 ≤30 字的归因理由 + 1–2 条 actionable 后续观察建议",
        "store":  "归因结果通过 anomaly_memory_remember 写入 ChromaDB",
        "next":   "拼成异动卡片，通过 chat_message 推送给用户",
    },
}


# render events keyed by card
_RENDER: dict[str, Explanation] = {
    "portfolio_snapshot": {
        "source": "EDGAR 取回并 CUSIP 聚合后的持仓列表 + Filing 元数据",
        "how":    "Python 按市值排序取前 N，拼 HTML <b>+<code>+<i> 富文本",
        "what":   "Telegram 风格组合卡：Top N 持仓 + 总组合价值 + 申报日",
        "store":  "不持久化（每次查询单独渲染）",
        "next":   "塞进 chat_message 事件，推送给前端聊天面板",
    },
    "institutional_summary": {
        "source": "InstitutionalReport 对象（新 filings + 显著变动总数）",
        "how":    "字符串模板拼接：管理人数 + 变动笔数 + 数据源使用量",
        "what":   "头部卡片：今日新 13F 几个 manager · 显著变动几笔",
        "store":  "不持久化",
        "next":   "作为 messages 数组的第 0 个元素，后面跟每个 manager 一张详情卡",
    },
    "manager_detail": {
        "source": "单个 manager 的 Filing + 该 manager 的 PositionChange 列表",
        "how":    "字符串模板：portfolio 概览 + 前 10 笔变动 + LLM interpretation",
        "what":   "单 manager 详情卡：组合规模 / 申报日 / 季度对比变动表",
        "store":  "不持久化",
        "next":   "和 institutional_summary 一起 join 成 chat_message 推送",
    },
    "anomaly_card": {
        "source": "Anomaly 对象 + Generator 输出的归因理由 + Tavily 新闻来源",
        "how":    "字符串模板：价格变动 + 板块相对强度 + 归因 + 数据来源",
        "what":   "异动卡：现价/涨跌 / 板块对比 chip / Top 3 归因 / 来源 URL 列表",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送；同时归因结果异步写入 ChromaDB RAG",
    },
}


# db_write events keyed by fn
_DB_WRITE: dict[str, Explanation] = {
    "save_filing": {
        "source": "Filing metadata + 已聚合的持仓列表",
        "how":    "SQLite INSERT OR REPLACE 到 edgar.db（WAL 模式，多服务可并发读）",
        "what":   "filings 表（主键 CIK + accession）+ positions 表（主键 accession + CUSIP）",
        "store":  "持久化到 data/edgar.db",
        "next":   "供 /holders 反查、周日 ④b backfill 校验、scheduler 推送去重",
    },
    "anomaly_memory_remember": {
        "source": "Anomaly 对象 + Generator 归因结果",
        "how":    "ChromaDB add()，先用 OpenAI text-embedding-3-small 把文本向量化",
        "what":   "异动事件 + 归因摘要（向量化嵌入，即把文本变成数字坐标，方便后续按相似度检索）",
        "store":  "持久化到 chroma/ 向量数据库",
        "next":   "未来用户问类似异动时，RAG 检索能召回历史归因做对照",
    },
    "archive_push": {
        "source": "已推送给用户的卡片完整文本 + 事件类型 + 时间戳",
        "how":    "SQLite INSERT 到 archive.db",
        "what":   "推送日志：何时给谁推了什么内容（事件类型 + ticker + 完整 HTML）",
        "store":  "持久化到 data/archive.db",
        "next":   "供离线查询脚本 query_archive 检索、未来做事件研究回测",
    },
}


# Per-responder explanations (module_enter events) keyed by responder function name.
_MODULE: dict[str, Explanation] = {
    "_r_thirteen_f": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.institutional_quick() 的轻量封装",
        "what":   "整体流程：EDGAR 抓 → CUSIP 聚合 → 季度对比 → LLM 解读 → 拼 3 张卡",
        "store":  "不持久化（read-only，不写 edgar.db）",
        "next":   "执行 5 步流程，最终通过 chat_message 推送 3 张卡片",
    },
    "_r_explain_move": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.monitoring.attribute() 的轻量封装",
        "what":   "整体流程：拉价格 → 检测异动 → 拉新闻 → Verifier 评级 → Generator 归因 → 写 RAG",
        "store":  "归因结果持久化到 ChromaDB",
        "next":   "执行 6 步流程，最终通过 chat_message 推送异动卡",
    },
}


# Meta events — single entries because there's only one "kind" per event_type.
_META: dict[str, Explanation] = {
    "intent_classified": {
        "source": "上一步 LLM 分类器的输出",
        "how":    "JSON 验证 + 白名单枚举校验，不合法的统一归类 unknown",
        "what":   "命中的 intent 名 + 提取的参数（ticker / manager 等）",
        "store":  "不持久化（每次查询重新分类）",
        "next":   "通过 DISPATCH 表路由到对应 _r_xxx responder 函数",
    },
    "chat_message": {
        "source": "responder 函数返回的完整 HTML 字符串",
        "how":    "通过 SSE 流式推送，Markdown 渲染显示",
        "what":   "聊天面板的完整回复（可能含多张卡片）",
        "store":  "不持久化（每次查询单独生成）",
        "next":   "session_end 事件汇总总成本和总耗时",
    },
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def _normalize_fd_endpoint(endpoint: str) -> str:
    """Strip the FD client class prefix so 'CachedFDClient.get_prices' →
    'get_prices'. EDGAR endpoints (Company.get_filings, Filing.obj) keep
    their class prefix because that's how the user's spec listed them.
    """
    if "." in endpoint and endpoint.split(".")[0].endswith("FDClient"):
        return endpoint.split(".", 1)[1]
    return endpoint


_LLM_PROMPT_FINGERPRINTS: list[tuple[str, str]] = [
    # (substring, role). Longest / most distinctive substrings first.
    ("意图分类器", "intent_classifier"),
    ("严苛的金融分析师", "verifier"),
    ("归因理由的因果链", "verifier"),
    ("股票异动归因分析师", "generator"),
    ("机构持仓分析师", "interpret_changes"),
]


def _llm_role(event: dict[str, Any]) -> Optional[str]:
    """Identify which v2 prompt this LLM call came from by fingerprinting
    the prompt text. Returns None if no match (e.g., a future prompt we
    haven't seen).
    """
    prompt = str(event.get("prompt_preview") or "")
    for needle, role in _LLM_PROMPT_FINGERPRINTS:
        if needle in prompt:
            return role
    return None


# Public alias — the executor attaches this to llm_call events so the
# frontend's pipeline mapper can read `event.role` directly.
llm_role = _llm_role


def lookup(event: dict[str, Any]) -> Optional[Explanation]:
    """Return the explanation for one event, or None if we don't have one."""
    et = event.get("type")
    if et == "api_call":
        provider = str(event.get("provider") or "")
        endpoint = str(event.get("endpoint") or "")
        if provider == "fd":
            endpoint = _normalize_fd_endpoint(endpoint)
        return _API.get((provider, endpoint))
    if et == "transform":
        op = str(event.get("op") or "")
        return _TRANSFORM.get(op)
    if et == "render":
        card = str(event.get("card") or "")
        return _RENDER.get(card)
    if et == "db_write":
        fn = str(event.get("fn") or "")
        return _DB_WRITE.get(fn)
    if et == "module_enter":
        name = str(event.get("name") or "")
        return _MODULE.get(name)
    if et == "llm_call":
        role = _llm_role(event)
        if role is None:
            return None
        return _LLM_BY_ROLE.get(role)
    if et in _META:
        return _META[et]
    return None


__all__ = ["Explanation", "lookup", "llm_role"]
