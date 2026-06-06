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
    ("ark_csv", "fetch_holdings"): {
        "source": "ARK Invest 官网每日发布的持仓 CSV（公开免费）",
        "how":    "HTTP GET 拉单个 ETF 的 CSV，解析后归一化字段",
        "what":   "当日全量持仓：ticker / 公司名 / 股数 / 市值 / 仓位百分比",
        "store":  "不持久化（ETF 数据库的 save_snapshot 步骤再写）",
        "next":   "对比昨日快照算单日调仓差异，识别新进/减持/清仓",
    },
    ("alpaca", "get_account"): {
        "source": "Alpaca paper 账户接口",
        "how":    "HTTP GET 带 API key + secret；alpaca-py 库封装",
        "what":   "账户余额：组合价值 / 现金 / 购买力 / paper 标记",
        "store":  "不持久化",
        "next":   "拼进 /portfolio 卡片或 /pnl 卡片的头部",
    },
    ("alpaca", "get_all_positions"): {
        "source": "Alpaca paper 账户的持仓接口",
        "how":    "HTTP GET 带 API key；返回所有未平仓头寸",
        "what":   "每只标的：股数 / 平均成本 / 现价 / 未实现盈亏",
        "store":  "不持久化",
        "next":   "和 get_account 一起拼成 /portfolio 或 /pnl 完整卡片",
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
    "etf_diff": {
        "source": "今天和昨天两份 ARK 持仓快照",
        "how":    "Python 按 ticker 配对，算单日股数变动百分比",
        "what":   "调仓表：新进 / 减持 / 清仓 + 仓位百分比变化",
        "store":  "不持久化（来源数据已经在 etf.db）",
        "next":   "送入 ETF 卡片格式化器，作为「24h 变动」段落显示",
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
    "filter": {
        "source": "上一步 LLM 提议或检测器的候选列表",
        "how":    "Python 阈值判断 + Tavily 共现验证，淘汰未通过的候选",
        "what":   "通过过滤的候选 + 各类淘汰原因计数",
        "store":  "不持久化",
        "next":   "通过的候选进入下一步（产业链卡片渲染、推送等）",
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
    "proposer": {
        "source": "用户输入的种子 ticker（产业链查询）",
        "how":    "DeepSeek-chat 严格 prompt：列出每只种子的供应商/客户/同业/受益方",
        "what":   "JSON：每个种子配 N 个邻居 ticker + 关系标签",
        "store":  "不持久化",
        "next":   "邻居关系交给 Tavily 共现验证，没在同篇新闻共现的丢弃",
    },
    "narrator": {
        "source": "筛选器通过的候选股 + 当周相对同业表现 + Tavily 新闻摘要",
        "how":    "DeepSeek-chat 模板填充模式：模型只写定性短语，数字由 Python 注入",
        "what":   "每只股票一组 bull + bear 各 ≤30 字判断",
        "store":  "不持久化",
        "next":   "和数字一起拼成 /summary 卡片显示给用户",
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
    "lateral_result": {
        "source": "Proposer 提议 + Tavily 验证后通过的邻居列表",
        "how":    "字符串模板：种子 ticker 一组 + 4 类邻居（供应商/客户/同业/受益方）",
        "what":   "产业链卡片：每个种子下面挂通过验证的邻居 + 关系标签",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "summary_card": {
        "source": "FD 多端点（价格 / 财务 / 财报 / 内部人）+ Tavily 新闻摘要",
        "how":    "字符串模板：把多维数据拼成五段式个股快照",
        "what":   "个股综合卡：价格走势 + 关键财务 + 财报 surprise + 内部人 + 近期新闻",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "holders_card": {
        "source": "edgar.db 跨 10 位 manager 的当前持仓查询结果",
        "how":    "Python 按市值排序持有方，未持有的也列出来对比",
        "what":   "反查卡片：哪些 manager 持有该 ticker + 持仓规模/占比 + 已退出的 manager",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "etf_snapshot": {
        "source": "ARK CSV 当日持仓 + 与昨日的 etf_diff 结果",
        "how":    "字符串模板：今日 Top N 持仓 + 24h 调仓变动",
        "what":   "ETF 卡片：基金标识 / 快照日期 / Top N 持仓 / 单日调仓表",
        "store":  "不持久化（来源数据已写入 etf.db）",
        "next":   "通过 chat_message 推送给用户",
    },
    "portfolio_card": {
        "source": "Alpaca get_account + get_all_positions 返回结果",
        "how":    "字符串模板：账户头部 + 每只持仓一行",
        "what":   "Alpaca 持仓卡：现金 / 组合价值 / 购买力 / 各只持仓盈亏",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "pnl_card": {
        "source": "Alpaca 账户 + 持仓快照",
        "how":    "Python 计算当日开仓-现价收益 + 多空敞口",
        "what":   "P&L 卡片：当日总盈亏 + 各持仓单独盈亏 + 多空敞口",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "alerts_list": {
        "source": "bot_state.db 的 alerts 表（未触发的提醒）",
        "how":    "SQL SELECT WHERE fired_at IS NULL；按 ID 排序",
        "what":   "提醒列表卡片：每条提醒 ID / ticker / 方向 / 目标价格",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "watchlist_card": {
        "source": "bot_state.db 的 watchlist 表",
        "how":    "SQL SELECT，按添加时间排序",
        "what":   "关注列表卡：每只 ticker + 添加日期 + 用户备注",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "anomalies_list": {
        "source": "archive.db 的最近异动推送记录（盘后 ② 异动监测的输出）",
        "how":    "SQL SELECT 最近 N 天的 anomaly 类型事件，按时间倒序",
        "what":   "近期异动列表：每条异动 ticker + 日期 + 简短 chip",
        "store":  "不持久化",
        "next":   "通过 chat_message 推送给用户",
    },
    "settings_card": {
        "source": "MonitorConfig + DEFAULT_FILTERS + LATERAL_FILTERS 三组静态阈值",
        "how":    "字符串模板把每组配置打印成易读的中文段落",
        "what":   "只读设置卡：异动阈值 / 筛选阈值 / 产业链扩展阈值",
        "store":  "不持久化（配置静态）",
        "next":   "通过 chat_message 推送给用户",
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
    "save_snapshot": {
        "source": "ARK CSV fetch_holdings 解析出的当日持仓",
        "how":    "SQLite INSERT OR REPLACE 到 etf.db",
        "what":   "etf_snapshots 表：（基金 + 日期 + ticker）+ 股数 + 市值 + 仓位百分比",
        "store":  "持久化到 data/etf.db",
        "next":   "下次同基金查询时，作为「昨日」基准供 etf_diff 对比",
    },
    "remember": {
        "source": "Anomaly 对象 + Generator 归因结果",
        "how":    "ChromaDB add()，先用 OpenAI text-embedding-3-small 把文本向量化",
        "what":   "异动事件 + 归因摘要（向量化嵌入，即把文本变成数字坐标，方便后续按相似度检索）",
        "store":  "持久化到 chroma/ 向量数据库",
        "next":   "未来用户问类似异动时，RAG 检索能召回历史归因做对照",
    },
    "alert_add": {
        "source": "用户输入的 ticker + 方向 + 目标价格（已通过校验）",
        "how":    "SQLite INSERT 到 bot_state.db 的 alerts 表",
        "what":   "新建一条 alert：自增 ID / ticker / 方向 / 目标价格 / 创建时间",
        "store":  "持久化到 data/bot_state.db",
        "next":   "streamer 服务每分钟轮询时会看到这条新提醒",
    },
    "alert_remove": {
        "source": "用户指定的 alert ID",
        "how":    "SQLite DELETE FROM alerts WHERE id=?",
        "what":   "删除该 ID 的提醒（如果不存在则 0 行受影响）",
        "store":  "持久化到 data/bot_state.db",
        "next":   "下次 streamer 轮询不再看到该提醒",
    },
    "watchlist_add": {
        "source": "用户输入的 ticker（已通过校验）+ 可选备注",
        "how":    "SQLite INSERT 到 bot_state.db 的 watchlist 表（重复添加会被忽略）",
        "what":   "watchlist 表新增一行：ticker / 添加时间 / 备注",
        "store":  "持久化到 data/bot_state.db",
        "next":   "盘后 agent 推送时会优先关注 watchlist 里的 ticker",
    },
    "watchlist_remove": {
        "source": "用户指定的 ticker",
        "how":    "SQLite DELETE FROM watchlist WHERE ticker=?",
        "what":   "删除该 ticker 的关注（如果不存在则 0 行受影响）",
        "store":  "持久化到 data/bot_state.db",
        "next":   "盘后 agent 不再特别推送该 ticker",
    },
    "archive_push": {
        "source": "已推送给用户的卡片完整文本 + 事件类型 + 时间戳",
        "how":    "SQLite INSERT 到 archive.db",
        "what":   "推送日志：何时给谁推了什么内容（事件类型 + ticker + 完整 HTML）",
        "store":  "持久化到 data/archive.db",
        "next":   "供离线查询脚本 query_archive 检索、未来做事件研究回测",
    },
}


# db_read events keyed by table or function name (we accept both since the
# emit site may carry either field). The hook layer doesn't auto-instrument
# read paths; emits are placed manually in v2/bot/responders.py.
_DB_READ: dict[str, Explanation] = {
    "edgar.db": {
        "source": "data/edgar.db 的 filings + positions 表",
        "how":    "SQLite SELECT；遍历 10 位 manager 找该 ticker 的当前持仓",
        "what":   "每个 manager 的当前持仓行（股数 / 市值 / 占比 / 季度）",
        "store":  "只读（不写回）",
        "next":   "按市值排序后送入 holders_card 渲染",
    },
    "etf.db": {
        "source": "data/etf.db 的 etf_snapshots 表",
        "how":    "SQLite SELECT 最近一天的快照（按日期倒序 LIMIT 1）",
        "what":   "昨日完整持仓列表 + 快照日期，用作今日对比基准",
        "store":  "只读",
        "next":   "和今日 fetch_holdings 结果一起送入 compute_daily_changes()",
    },
    "bot_state.db": {
        "source": "data/bot_state.db 的 watchlist / alerts / settings 表",
        "how":    "SQLite SELECT，按用途单独一句 SQL",
        "what":   "用户关注列表 / 未触发提醒 / 个性化配置",
        "store":  "只读",
        "next":   "送入对应卡片渲染器（watchlist_card / alerts_list / settings_card）",
    },
    "archive.db": {
        "source": "data/archive.db 的事件推送日志",
        "how":    "SQLite SELECT 最近 N 天 type='anomaly' 的记录",
        "what":   "近期异动事件列表（ticker / 日期 / chip 摘要）",
        "store":  "只读",
        "next":   "送入 anomalies_list 卡片渲染",
    },
}


# validate events keyed by `what` field
_VALIDATE: dict[str, Explanation] = {
    "ticker": {
        "source": "用户输入的字符串",
        "how":    "正则 + 长度判断：纯字母 + 长度 2-5",
        "what":   "通过则标准化为大写 ticker；不通过返回友好错误",
        "store":  "不持久化",
        "next":   "通过的 ticker 进入后续 SQL 写入或外部 API 调用",
    },
    "price": {
        "source": "用户输入的数字 / 方向字符串",
        "how":    "Python 类型转换 + 范围检查（>0 + 方向 ∈ {above, below}）",
        "what":   "标准化的浮点价格 + 方向枚举",
        "store":  "不持久化",
        "next":   "送入 bot_state.alert_add() 写到 alerts 表",
    },
}


# Per-responder explanations (module_enter events) keyed by responder name.
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
        "what":   "整体流程：拉价格 → 拉新闻 → Generator 归因 → Verifier 评级 → 写 RAG → 拼卡",
        "store":  "归因结果持久化到 ChromaDB",
        "next":   "执行 6 步流程，最终通过 chat_message 推送异动卡",
    },
    "_r_chain": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.lateral 的产业链横向扩展 pipeline",
        "what":   "整体流程：DeepSeek 提议邻居 → Tavily 共现验证 → 阈值过滤 → 拼卡",
        "store":  "不持久化",
        "next":   "执行 4 步流程，最终通过 chat_message 推送产业链卡",
    },
    "_r_summary": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.summary() 的轻量封装",
        "what":   "整体流程：拉价格 + 财务 + 财报 + 内部人 + 新闻 → LLM 拼叙述",
        "store":  "不持久化（read-only）",
        "next":   "执行 6 步流程，最终通过 chat_message 推送综合卡",
    },
    "_r_holders_view": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.holders() 的轻量封装",
        "what":   "整体流程：遍历 10 位 manager 查 edgar.db → 排序 → 拼卡",
        "store":  "不持久化（read-only，不连 EDGAR）",
        "next":   "执行 3 步流程，最终通过 chat_message 推送反查卡",
    },
    "_r_etf_view": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.etf_view() 的轻量封装",
        "what":   "整体流程：拉 CSV → 跟昨日 diff → 写 etf.db → 拼卡",
        "store":  "持久化今日快照到 etf.db",
        "next":   "执行 4 步流程，最终通过 chat_message 推送 ETF 卡",
    },
    "_r_portfolio_view": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.portfolio_view() 的轻量封装",
        "what":   "整体流程：Alpaca get_account + get_all_positions → 拼卡",
        "store":  "不持久化",
        "next":   "执行 2 步流程，最终通过 chat_message 推送持仓卡",
    },
    "_r_pnl_view": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.pnl_view() 的轻量封装",
        "what":   "整体流程：Alpaca get_account + get_all_positions → 算 P&L → 拼卡",
        "store":  "不持久化",
        "next":   "执行 3 步流程，最终通过 chat_message 推送 P&L 卡",
    },
    "_r_alert_set": {
        "source": "前一步 intent 分类的结果 + 用户输入的 ticker/价格/方向",
        "how":    "调用 v2.bot.responders.alert_set() 的轻量封装",
        "what":   "整体流程：校验输入 → INSERT 到 bot_state.db → 返回确认卡",
        "store":  "持久化新提醒到 bot_state.db",
        "next":   "执行 3 步流程，最终通过 chat_message 推送确认卡",
    },
    "_r_alert_list": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.alert_list_view() 的轻量封装",
        "what":   "整体流程：SELECT 未触发提醒 → 拼成列表卡",
        "store":  "不持久化（read-only）",
        "next":   "执行 2 步流程，最终通过 chat_message 推送提醒列表",
    },
    "_r_alert_remove": {
        "source": "前一步 intent 分类的结果 + 用户指定的 alert ID",
        "how":    "调用 v2.bot.responders.alert_remove_view() 的轻量封装",
        "what":   "整体流程：DELETE 该 ID → 返回确认/错误卡",
        "store":  "持久化删除到 bot_state.db",
        "next":   "执行 2 步流程，最终通过 chat_message 推送结果",
    },
    "_r_watchlist_view": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.state.watchlist_list() 的轻量封装",
        "what":   "整体流程：SELECT watchlist → 按时间排序 → 拼卡",
        "store":  "不持久化（read-only）",
        "next":   "执行 2 步流程，最终通过 chat_message 推送关注列表",
    },
    "_r_watchlist_add": {
        "source": "前一步 intent 分类的结果 + 用户输入的 ticker",
        "how":    "调用 v2.bot.state.watchlist_add() 的轻量封装",
        "what":   "整体流程：校验 ticker → INSERT 到 watchlist → 返回确认卡",
        "store":  "持久化新关注到 bot_state.db",
        "next":   "执行 3 步流程，最终通过 chat_message 推送确认",
    },
    "_r_watchlist_remove": {
        "source": "前一步 intent 分类的结果 + 用户指定的 ticker",
        "how":    "调用 v2.bot.state.watchlist_remove() 的轻量封装",
        "what":   "整体流程：DELETE FROM watchlist WHERE ticker=? → 返回结果",
        "store":  "持久化删除到 bot_state.db",
        "next":   "执行 2 步流程，最终通过 chat_message 推送结果",
    },
    "_r_settings": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.bot.responders.settings_view() 的轻量封装",
        "what":   "整体流程：读取静态配置常量 → 拼成只读设置卡",
        "store":  "不持久化（配置不写）",
        "next":   "执行 2 步流程，最终通过 chat_message 推送设置卡",
    },
    "_r_find_anomalies": {
        "source": "前一步 intent 分类的结果",
        "how":    "调用 v2.archive 的近期推送查询",
        "what":   "整体流程：SELECT archive.db 近 N 天 anomaly 事件 → 拼成列表卡",
        "store":  "不持久化（read-only）",
        "next":   "执行 2 步流程，最终通过 chat_message 推送异动列表",
    },

    # Cron responder names — emitted by capture_trace_with_framing in the
    # scheduler scripts. Same fields as the on-demand _r_* entries above.
    "_r_etf_snapshot": {
        "source": "scheduler 触发，无用户输入",
        "how":    "调用 v2.etf 的完整流程（fetch CSV → 算变动 → 写入 → 渲染）",
        "what":   "整体流程：拉 ARK 4 只基金的最新持仓 → 对比昨日 → 写 SQLite snapshot → 渲染推送卡",
        "store":  "持久化到 data/etf.db（每日累积时间序列）",
        "next":   "通过 chat_message emit 推送到 Telegram + archive.db",
    },
    "_r_anomaly_monitor": {
        "source": "scheduler 触发（17:35 ET），扫描 TECH_30 universe",
        "how":    "调用 v2.monitoring 检测异动并对每个 ticker 跑 attribute()",
        "what":   "整体流程：检测器找异动 → 对每个异动拉新闻 → Generator 写归因 → Verifier 评级 → 写 ChromaDB",
        "store":  "归因结果持久化到 chroma/；推送日志写 archive.db",
        "next":   "对每个异动通过 chat_message emit 推送一张卡片",
    },
    "_r_daily_screen": {
        "source": "scheduler 触发（17:30 ET），全 TECH_30 universe",
        "how":    "调用 v2.screening 的硬规则筛选 + DeepSeek narrator",
        "what":   "整体流程：拉 30 个 ticker 基本面 → 硬规则过滤 → 通过的让 LLM 写一句话 bull/bear → 拼综合卡",
        "store":  "不持久化（每日重新筛选）",
        "next":   "通过 chat_message emit 推送给用户",
    },
    "_r_lateral_expansion": {
        "source": "scheduler 触发（周一 18:00 ET），用前一天异动 ticker 当种子",
        "how":    "调用 v2.lateral：DeepSeek proposer 提议邻居 → Tavily 共现验证 → 阈值过滤",
        "what":   "整体流程：每个种子识别 4 类邻居（供应商/客户/同业小盘/受益方）→ Tavily 验证 → 过滤 → 拼卡",
        "store":  "不持久化",
        "next":   "通过 chat_message emit 推送产业链卡",
    },
    "_r_institutional": {
        "source": "scheduler 触发（周二/周五 18:00 ET），10 位明星 manager",
        "how":    "调用 v2.institutional 完整 pipeline（EDGAR → CUSIP → diff → LLM interpret）",
        "what":   "整体流程：每个 manager 拉最新 13F-HR → CUSIP 聚合 → 跟前季度对比 → top 20 变动让 DeepSeek 解读 → 拼 N 张卡（summary + 每 manager 一张）",
        "store":  "持久化到 data/edgar.db",
        "next":   "通过多次 chat_message emit 推送 1 张总览 + 每 manager 一张详情",
    },

    # On-demand portfolio queries (bot / dashboard QA, NOT cron)
    "_r_risk_view": {
        "source": "用户实时请求（/risk 或 NL「我的组合风险」），同步走 v2.portfolio.build_risk_report",
        "how":    "同 ⑨ cron 一样 fan out 到 6 个子模块（positions / concentration / exposure / pnl / drawdown / earnings_risk），失败 fall back 到 None 字段 + warning",
        "what":   "RiskReport 全量字段渲染为单卡（组合价值 / 集中度 / 暴露 / 1M 回撤 / 7d 财报）",
        "store":  "read-only — 不写 archive，不算 priority（priority 只跟 cron-pushed 卡片相关）",
        "next":   "用 ⑨ 同款 inline _format_risk_card 渲染，Telegram / dashboard 单条回复",
    },
    "_r_pnl_period": {
        "source": "用户实时请求（/pnl week|month 或 NL「这周亏了多少」），调 v2.portfolio.compute_pnl",
        "how":    "compute_pnl 内部 fan out: get_pnl() 拿当日, get_portfolio_history(1M, 1D) 拿历史 1W / 1M 回报",
        "what":   "单期 P&L 摘要：本周 / 本月 fraction + 当日参考；账户史不足时显示 '数据不足'",
        "store":  "不持久化",
        "next":   "渲染为单卡，read-only 回复",
    },

    # SEC monitoring agent — Phase 3
    "_r_sec_8k": {
        "source": "scheduler 触发（17:05 ET Mon-Fri），watchlist + Alpaca 持仓 的合并 ticker 集合 + SEC EDGAR",
        "how":    "edgartools Company(ticker).get_filings(form='8-K', filing_date=today) → 解析 .items 拿 item codes → 按 Stage-0 优先级表分级",
        "what":   "EightKEvent 含全部 items 及各自评级（P0-P3）。5.02 调 LLM template-fill 抽取 CEO/CFO 姓名 confirm senior exec",
        "store":  "推送写 archive.db pushes 表",
        "next":   "max(items) 决定卡片 tier，2.02-only filings skip（⑧ Earnings Summaries 处理）",
    },
    "_r_sec_form4": {
        "source": "scheduler 触发（17:45 ET Mon-Fri），watchlist + Alpaca 持仓 + SEC EDGAR Form 4",
        "how":    "edgartools Form4.to_dataframe()['Code'] 拿 transaction code → P/S 个人卡片 / A/M/F/G/C 进 noise_summary / 同日同向 ≥3 distinct insiders 算 cluster",
        "what":   "Form4Transaction (P/S signal) + Form4Cluster + noise_summary。priority 按 USD magnitude + insider role (CEO/CFO +10) + 10b5-1 plan (-10) 算",
        "store":  "推送写 archive，A/M/F/G/C noise 暂存等 Phase 3.5 weekly digest 消费",
        "next":   "cluster 卡先推（更重要），再推单笔 signal 卡。同 cluster 内的单笔不重复推送",
    },
    # On-demand SEC bot queries (Phase 3 Stage 4) — read-only, not cron
    "_r_eight_k_view": {
        "source": "用户实时请求（/8k AAPL 或 NL「AAPL 最近 8-K」），单 ticker SEC EDGAR 拉过去 30 天 8-K",
        "how":    "edgartools.Company(ticker).get_filings(form='8-K', filing_date=(today-30d, today)) → 解析 items → 5.02 调 LLM template-fill",
        "what":   "多 filing 单卡：每个 filing 列出所有 items 及评级 + 5.02 抽取的 CEO/CFO 姓名（LLM 失败显示 '(姓名待解析)' 占位）",
        "store":  "read-only — 不写 archive，不算 priority（priority 只跟 cron-pushed 卡片相关）",
        "next":   "渲染为单一回复卡，Telegram / dashboard 单条返回",
    },
    # On-demand macro bot queries (Phase 4 Stage 4) — read-only, not cron
    "_r_macro_view": {
        "source": "用户实时请求（/macro 或 NL「宏观怎么样 / market 状态」），调 build_macro_snapshot + release_calendar 窗口",
        "how":    "yfinance VIX/DXY/WTI/Gold + FRED canonical rates (DGS2/DGS10/T10Y2Y/Fed Funds) → 与 ⑭ cron 同源；同时 get_releases_in_window 拉过去 14 天 + 未来 30 天 release schedule",
        "what":   "多段卡片：市场状态 + 收益率 + 最近 release + 下次 release。warnings list 在数据源失败时显示「数据待入库」",
        "store":  "read-only — 不写 archive，不算 priority（priority 只跟 cron-pushed 卡片相关）",
        "next":   "渲染为单一回复卡，Telegram / dashboard 单条返回",
    },
    "_r_release_check": {
        "source": "用户实时请求（/cpi / /fomc / NL「最近 CPI」「上次 FOMC」），按 release_type 枚举（cpi/pce/nfp/gdp/ppi/claims/fomc）",
        "how":    "release_calendar 找到最近一个该 release_type 的 release date → build_release_event(that_date) 拉 FRED + summarizer LLM（Layer 1+2 defense）。FOMC 单独走 fomc_parser + tavily_consensus（Layer 3：Python diff + sell-side majority vote，绝不让 LLM 判鹰鸽）",
        "what":   "单条 release 卡：数据点（Python 算）+ LLM 定性标签（bull/bear/narrative/tone，≤40 字，禁止数字泄漏）+ 下次发布提示。FOMC 卡含 statement diff（新增/移除 phrases）+ SEP dot plot + Tavily 来源",
        "store":  "read-only",
        "next":   "渲染为单一回复卡",
    },
    "_r_insider_view": {
        "source": "用户实时请求（/insiders NVDA 或 NL「NVDA 内部人交易」），单 ticker SEC EDGAR 拉过去 N 天 Form 4（默认 90 天）",
        "how":    "edgartools Form4.to_dataframe()['Code'] 全量解析 → 按 P/S/A/M/F/G/C 分桶 → cluster.find_clusters 滚动窗口 ≥3 distinct insiders",
        "what":   "P/S 列具体笔（含 USD + 申报人 + 10b5-1 标记），A/M/F/G/C 仅显示 count，cluster 单独列出",
        "store":  "read-only — 不写 archive，不算 priority，不调 LLM",
        "next":   "单一汇总卡返回",
    },

    # Portfolio risk agent — Phase 2
    "_r_portfolio_risk": {
        "source": "scheduler 触发（18:30 ET Mon-Fri），Alpaca 当前持仓 + portfolio_history(1M, 1D) + yfinance 7d 财报日历",
        "how":    "build_risk_report 串联 6 个独立子模块（positions / concentration / exposure / pnl / drawdown / earnings_risk），任何一个失败都 fall back 到 None 字段 + warning 字串",
        "what":   "RiskReport 含组合价值/现金、Top-1/3/5 + HHI、行业 ETF 暴露、日/周/月 P&L、1M 回撤、7d 财报风险",
        "store":  "推送本身写入 archive.db pushes 表（无新表）",
        "next":   "compute_importance 按 6 个 metadata 算 priority（daily_pnl ≤ -5% 升 P0），推 Telegram + dashboard auto-push",
    },
    "_r_portfolio_weekly": {
        "source": "scheduler 触发（Fri 19:00 ET），同 ⑨ 的 RiskReport + 额外 portfolio_history(1M, 1D) 渲图 + Phase 2.5 full 读 positions_snapshot 最近 7 天",
        "how":    "复用 build_risk_report 拿组合数字，再用 matplotlib 渲染 1M equity curve PNG（标峰值位置）。read_weekly_window + compute_weekly_attribution 算 per-position 周表现归因",
        "what":   "周/月回报 + 1M 最大回撤 + 主要行业暴露 + per-position 表现归因（最佳/最差/净贡献，<5 天显示'累积中'）+ 下周财报清单 + 权益曲线图",
        "store":  "image 写到 data/images/，元数据 + caption 入 archive.db",
        "next":   "推 Telegram photo + dashboard auto-push；priority 强制 P1 底线（周报性质，operator 需看见）",
    },
    "_r_positions_snapshot": {
        "source": "scheduler 触发（⑨b Mon-Fri 16:25 ET），Alpaca 当前持仓",
        "how":    "get_flat_positions(broker) → write_daily_snapshot(positions, today_iso, archive)。INSERT OR REPLACE on (snapshot_date, ticker) — 同日 rerun 覆盖不重复",
        "what":   "positions_snapshot 表每持仓一行 (snapshot_date / ticker / market_value / weight / sector_etf)",
        "store":  "positions_snapshot 表（archive.db 新增 Phase 2.5 full table），无 Telegram push",
        "next":   "⑩ Fri 19:00 ET 读最近 7 天窗口，算 per-position weekly attribution",
    },

    # Phase 4 Macro Agent — ⑭⑮⑯⑰
    "_r_macro_snapshot": {
        "source": "scheduler 触发（16:30 ET Mon-Fri），yfinance VIX/DXY/WTI/Gold + FRED canonical Fed Funds / DGS2 / DGS10 / T10Y2Y / VIXCLS",
        "how":    "build_macro_snapshot 拉两路独立数据：market_client(yfinance) + fred_client(REST/wrapper)。任何单点失败聚合到 warnings list，不阻塞",
        "what":   "MacroSnapshot 含 VIX 当日 + 1d% / DXY / WTI / Gold / 2Y / 10Y / T10Y2Y + 4 个 anomaly flags（vix_spike +20% / vix_elevated +10% / curve_flip / rates_shocked ≥20bps）",
        "store":  "推送本身写 archive.db pushes 表",
        "next":   "anomaly flags 决定 priority kind：vix_spike→macro_vix_spike (P0) / curve_flip→macro_curve_flip (P1) / 否则 macro_snapshot_p3 (P3 默认日常背景)",
    },
    "_r_macro_release": {
        "source": "scheduler 触发（09:00 ET Mon-Fri），release_calendar.get_release_today 查今天有哪些 release（CPI/PCE/NFP/GDP/PPI/FOMC）",
        "how":    "无 release → 静默退出。每个 release：FRED 拉对应 series → transforms 算 mom/yoy/trend → summarizer LLM template-fill（Layer 1 prompt + Layer 2 regex reject 预测性短语和数字泄漏）。FOMC 走专用路径：fomc_parser 做 statement diff + dot-plot 提取（Python only）+ tavily_consensus 抓 sell-side hawkish/dovish majority vote（绝不让 LLM 出鹰鸽判断）",
        "what":   "MacroReport 含 today_releases（每条 MacroRelease 含 headline/core/mom/yoy/consensus/surprise_sigma/surprise_label/trend + bull/bear/narrative/tone 标签）和 fomc_event（含 statement_diff / sep_median_dots / sep_dot_plot_change / sell_side_sentiment）",
        "store":  "推送写 archive.db pushes 表",
        "next":   "compute_importance 按 surprise_sigma 升级：≥3σ→P0 / 2σ→+10 / 1σ→+5 / FOMC SEP shift→+15",
    },
    "_r_macro_claims": {
        "source": "scheduler 触发（Thu 09:30 ET），FRED ICSA 周度初请失业金数据",
        "how":    "build_claims_event 拉 ICSA series → headline = latest weekly print, core = 4-week moving average smoothed level。星期四 deterministic 触发（不查 release_calendar，trigger 已 gating）",
        "what":   "MacroRelease(release_type='Claims') 含 weekly 数 + 4-week MA + trailing trend 标签",
        "store":  "推送写 archive.db pushes 表",
        "next":   "默认 macro_release_p2 (P2)。surprise_sigma > 2 升 P1。假期周（Thanksgiving / 跨年）ICSA 无数据 → silent skip",
    },
    "_r_macro_weekly": {
        "source": "scheduler 触发（Fri 19:30 ET，错开 ⑨ 18:30 / ⑩ 19:00），release_calendar 本周已发生 + 下周预告 + FRED 1W deltas",
        "how":    "build_weekly_recap 拉 (week_start, today) 的已发布列表 + (today, today+7d) 的预告 + 4 个核心指标（VIX/DGS10/DGS2/T10Y2Y）的 1W 净变化",
        "what":   "dict 含 this_week_releases / next_week_releases / weekly_deltas（每个 series 1W delta float）",
        "store":  "推送写 archive.db pushes 表",
        "next":   "固定 macro_weekly (P1 floor) — operator 周末复盘视图，priority 不因数据多寡变化",
    },

    # Earnings agent — Phase 1
    "_r_earnings_reminders": {
        "source": "scheduler 触发（08:00 ET Mon-Fri），watchlist + Alpaca 持仓 的合并 ticker 集合",
        "how":    "yfinance 拿每只股票下次财报日，比对今天算 D-3/D-1/D-0",
        "what":   "需要提醒的 EarningsEvent 列表 + 估算 EPS / 营收",
        "store":  "不持久化（每天重算，自动吸收公司更改的发布日）",
        "next":   "按距离 + 是否持仓打 priority，逐条推送 Telegram + archive.db",
    },
    "_r_earnings_summaries": {
        "source": "scheduler 触发（21:00 ET Mon-Fri），今日发财报的 ticker + archive earnings_summarized 表去重",
        "how":    "FD 拿 actual vs estimate，LLM 写定性总结（数字 Python 注入，Template Fill 模式）",
        "what":   "EarningsSummary 卡（surprise / 4Q streak / bull-bear / 电话会链接）或 pending 占位",
        "store":  "summary 推送后写 archive.earnings_summarized 防止重复；pending 也记一行",
        "next":   "推 Telegram + dashboard 自动推送 feed；下一日 21:00 ET 再跑会自动 retry pending",
    },

    # Streamer 触发的两类推送 —— 不是 cron，而是分钟级实时轮询。
    "_r_alert_fire": {
        "source": "Streamer 1 分钟轮询发现 alerts 表里某条 target_price 被跨越",
        "how":    "Alpaca 实时价格 vs 用户设置目标价；SQL UPDATE WHERE fired_at IS NULL 保证只触发一次（原子性 one-shot）",
        "what":   "触发的 alert 元数据：ticker / 方向 / 目标价 / 触发时实际价格",
        "store":  "bot_state.db 的 alerts 表 fired_at 字段被原子置位",
        "next":   "格式化为 📈 价格提醒触发卡，推送到 Telegram + dashboard 自动推送 feed",
    },
    "_r_intraday_scan": {
        "source": "Streamer 1 分钟轮询的 TECH_30 universe 扫描（仅交易时段）",
        "how":    "对每个 ticker 取 Alpaca 实时价 + day bar 算双门槛（≥3% 涨跌 AND ≥2.5× 成交量节奏）",
        "what":   "通过双门槛的异动信号 + 板块 ETF 相对强度 chip（★ 逆势/同步）",
        "store":  "bot_state.db 的 intraday_cooldown 表标记 30 分钟冷却期",
        "next":   "渲染为 ⚡ 盘中异动卡（盘中刻意不调 LLM），推送到 Telegram + dashboard 自动推送 feed",
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


# Fingerprints + detector live in v2/observability/hooks.py — that's
# the layer that fires llm_call events, so role tagging happens at
# emit-time and cron-captured traces carry .role natively. We re-export
# a dict-friendly wrapper here for backward-compat callers (executor
# still imports llm_role from this module as a safety-net fallback).
from v2.observability import detect_llm_role as _detect_llm_role_str


def _llm_role(event: dict[str, Any]) -> Optional[str]:
    """Look up the role for an event whose role wasn't attached at emit
    time (e.g. older archive rows captured before this refactor). New
    emissions already carry .role from the hook layer.
    """
    return _detect_llm_role_str(str(event.get("prompt_preview") or ""))


# Public alias kept for the executor.sink fallback path.
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
    if et == "db_read":
        # Prefer db label (covers the watchlist/alerts/settings family that
        # share bot_state.db). Fall back to a table=... field if the emit
        # site carries one.
        db = str(event.get("db") or "")
        if db and db in _DB_READ:
            return _DB_READ[db]
        table = str(event.get("table") or "")
        if table and table in _DB_READ:
            return _DB_READ[table]
        return None
    if et == "validate":
        what = str(event.get("what") or "")
        return _VALIDATE.get(what)
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
