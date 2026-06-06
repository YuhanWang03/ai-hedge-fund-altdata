# AI Hedge Fund · 另类数据 Agent 系统

一个生产级的另类数据情报平台，以 Telegram bot 作为交付界面。**10 个已 ship 的 Phase** 把基本面筛选、异动检测、组合风险、SEC 监控、宏观数据、ARK 调仓等信号统一进 **21 个进程隔离的 cron 任务**，五重防幻觉机制保证每一条推送都有可追溯的源头。

定位为简历展示型项目，旨在展现端到端拥有"多源数据管道 + 双 LLM 校验架构 + 7×24 部署系统"的能力。

[English README](./README.md)

**Hero 数字**：10 Phases shipped · 21 cron jobs · 23 NL intents · 5 层防幻觉 · 474 个 sandbox tests · **~$22/月** 总运营成本

---

## 它能做什么

系统在一台 $6/月 的 VPS 上并发运行三个服务：**Scheduler**（21 个 cron 任务覆盖 08:00–21:00 ET）+ **Streamer**（盘中分钟级 alert + 异动扫描）+ **Telegram Bot**（25 slash + 23 NL intent）。三服务通过 WAL 模式共享 7 个 SQLite 数据库 + ChromaDB 向量库（RAG 记忆），单服务崩溃不污染其他。

### Cron Jobs 全表

按美东时间排序，21 个 agents（agent 详解见 [## 功能详解](#功能详解) → Cron Agents 各 family 子节）：

| 时间 (ET) | ID | 名称 | 频率 | Family | 推送 |
|---|---|---|---|---|---|
| **02:00 UTC** | ⑥ | Archive Cleanup | daily | 维护 | （维护，无推送） |
| **08:00** | ⑦ | Earnings Reminders | mon-fri | Earnings | ✅ |
| **08:30** | ⑬ | ARK Alerts | mon-fri | ARK | ✅ pre-market |
| **09:00** | ⑮ | Macro Release Scanner | mon-fri | Macro | ✅ 命中日 |
| **09:30** | ⑯ | Macro Initial Claims | **thu** | Macro | ✅ |
| **16:25** | ⑨b | Positions Snapshot | mon-fri | Portfolio | 🔇 archive only |
| **16:30** | ⑭ | Macro Daily Snapshot | mon-fri | Macro | ✅ |
| **16:45** | 📋 | P2 Digest | mon-fri | 维护 | ✅ 汇总 |
| **17:00** | ⑤ | ETF Daily Snapshot | mon-fri | 早期信号 | 🔇 dashboard only |
| **17:05** | ⑪ | SEC 8-K Scanner | mon-fri | SEC | ✅ |
| **17:30** | ① | Daily Screen | mon-fri | 早期信号 | ✅ |
| **17:35** | ② | Anomaly Monitor | mon-fri | 早期信号 | ✅ |
| **17:45** | ⑫ | SEC Form 4 Scanner | mon-fri | SEC | ✅ |
| **18:00** | ③ | Lateral Expansion | **mon** | 早期信号 | ✅ |
| **18:00** | ④ | Institutional 13F | **tue/fri** | 早期信号 | ✅ |
| **18:30** | ④b | 13F Backfill | **sun** | 早期信号 | 🔇 维护 |
| **18:30** | ⑨ | Portfolio Risk | mon-fri | Portfolio | ✅ |
| **19:00** | ⑩ | Portfolio Weekly | **fri** | Portfolio | ✅ |
| **19:15** | ⑫b | SEC Insider Weekly Digest | **fri** | SEC | ✅ |
| **19:30** | ⑰ | Macro Weekly Recap | **fri** | Macro | ✅ |
| **21:00** | ⑧ | Earnings Summaries | mon-fri | Earnings | ✅ |

**时间设计哲学**：晚 17:00–19:30 ET 是主推送密集窗口（盘后数据落地最快），08:00–09:30 ET 是早盘 pre-market 窗口（财报提醒 + ARK 调仓 + 宏观 release），16:25–16:45 ET 是收盘后 5–20 分钟的静默批量窗口（持仓快照 + macro snapshot + P2 汇总）。⑧ 财报总结 21:00 ET 因为 FD 财报数据当日通常 19:30–21:00 ET 才完整落地。

### 盘中 Streamer 实时推送

与 scheduler 并行运行。交易时段（周一-周五 9:30–16:00 ET）每 60 秒轮询一次，非交易时段 5 分钟一查。**盘中绝对不调用 LLM / Tavily** —— 深度归因留给 17:35 ET 盘后 ② Anomaly Monitor。详解见 [## 功能详解 → Streamer 盘中](#streamer-盘中)。

| 触发器 | 数据源 | 频率 | 何时触发 |
|---|---|---|---|
| 用户设置的价格提醒 | Alpaca `get_stock_latest_trade` | 1/min | `/alert NVDA 130 above` 或 "提醒我 NVDA 突破 130" 创建 |
| TECH_30 自动异动扫描 | Alpaca latest-trade × 29 ticker | 1/min | 双门槛：≥3% 价格变动 **AND** ≥2.5× 成交量节奏 |

### Telegram 主动查询

24/7 长轮询机器人，单用户授权（通过 chat ID 过滤），25 个 slash 命令 + 23 种 NL intent + 10 个 manager 别名。详解见 [## 功能详解 → Telegram 交互界面](#telegram-交互界面)。

| 类别 | Slash 命令 | NL 示例 |
|---|---|---|
| Watchlist | `/watchlist`, `/add NVDA`, `/remove TSLA` | "我关注了哪些股票" |
| 分析 | `/why`, `/summary`, `/chain`, `/13f`, `/holders`, `/etf` | "NVDA 为什么跌？", "Cathie 今天买啥" |
| 提醒 | `/alert`, `/alerts`, `/alert_remove` | "提醒我 NVDA 突破 130" |
| 账户 | `/portfolio`, `/pnl [day\|week\|month]`, `/risk` | "我的当日盈亏", "组合风险怎么样" |
| 财报 | `/earnings AAPL`, `/earnings`（日历）| "苹果什么时候发财报" |
| SEC | `/8k TICKER`, `/insiders TICKER [days]` | "NVDA 内部人交易" |
| Macro | `/macro`, `/cpi`, `/fomc`, `/yields` | "宏观怎么样", "最近 CPI", "上次 FOMC" |

NL 分类器走 DeepSeek temperature=0 + JSON 输出 + **白名单枚举校验**——任何不在 23 个固定 intent 内的输出 fallback 到 `unknown`，行为有界。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-scheduler.service                 │
│                       (21 cron jobs · 详见下表)                  │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-streamer.service                  │
│  9:30 - 16:00 ET 周一-周五 · 每 60 秒                            │
│   ├─ 用户 /alert 触发        · Alpaca latest-trade × ticker      │
│   └─ TECH_30 自动扫描        · 双门槛 + 板块逆势检测             │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                   共享 SQLite + ChromaDB                         │
│  archive.db     fd_cache.db     edgar.db     etf.db              │
│  bot_state.db   options.db      chroma/      screening_cache.db  │
└──────────────────────────────────────────────────────────────────┘
                              ▲
                              │
┌──────────────────────────────────────────────────────────────────┐
│                      hedge-fund-bot.service                      │
│  Slash 命令     · /why /summary /chain /13f /holders /etf        │
│                   /alert /alerts /alert_remove                   │
│                   /portfolio /pnl /risk /settings                │
│                   /watchlist /add /remove                        │
│                   /earnings /8k /insiders                        │
│                   /macro /cpi /fomc /yields                      │
│  NL 分类器      · 23 个意图 · DeepSeek T=0 · 严格枚举            │
└──────────────────────────────────────────────────────────────────┘

外部数据源：financialdatasets.ai · yfinance · SEC EDGAR · ARK CSV CDN
            Alpaca · FRED · Tavily News API · DeepSeek LLM · OpenAI embeddings
```

---

## 防幻觉 + Priority 系统

### 5 层信号防幻觉（system-wide）

LLM **从不直接产出数字事实**。它们只做分类决策、写定性叙述、在候选信源里做选择。**所有数字都来自原始数据源**。

| 层 | 机制 |
|---|---|
| 1. 实体过滤 | 新闻必须同时包含目标 ticker AND 至少一个公司名 token，才送给 Verifier |
| 2. 信源 tier 评分 | Verifier LLM 给每个信源打 Tier 1（SEC, Reuters, Bloomberg, WSJ, FT, CNBC）/ Tier 2（MarketWatch, SeekingAlpha, Yahoo, Fool, Forbes）/ Tier 3（其他）。**Tier 3 直接丢弃** |
| 3. Generator-Verifier 分离 | 独立的 Verifier LLM 验证 Generator 输出是否能用信源支撑，prompt 严禁出现输入未提及的数字 |
| 4. 模板填充模式 | 筛选叙述器只输出定性短语；Python f-string 注入所有数字。LLM 没机会读错指标 |
| 5. 过期数据 chip | filing > 180 天显示 ⚠️；> 365 天显示 ⚠️ 已 N 月未更新。防止用户把 2023 年的 13F 当作当前状态 |

### LLM 4 层 defense（Macro Agent 专用）

`v2/macro/summarizer.py` 单独叠加一套 macro-specific 防御，因为 release 解读对数字泄漏特别敏感：

| 层 | 位置 | 防御 |
|---|---|---|
| **L1 Template-Fill** | `summarizer._SYSTEM_PROMPT` | 严禁输出数字 + 严禁预测后市方向 + 4 个固定 JSON 字段（bull/bear/narrative/tone，每个 ≤40 字符） |
| **L2 Post-Parse Reject** | `summarizer._REJECT_PATTERNS` + `_DIGIT_LEAK_RE` | 正则扫描 "将/会/预期/预计/料/可能.*[上下涨跌升降]" + 数字泄漏（带 % / bps / 个百分点 后缀）→ fallback to neutral |
| **L3 FOMC bypass** | `fomc_parser` + `tavily_consensus` | 鹰鸽判断完全不调 LLM——Python diff 15 个 KEY_PHRASES + SEP dot plot 数字 extract + Tavily majority vote 8 trusted domains |
| **L4 Historical analog** | （deferred） | "类似 surprise 历史上 N 天后市场反应" 待 Phase 4.5 落地；当前实现承认 release 已发生，不预测后市 |

**保守降级**：LLM 失败 (timeout / parse fail / Layer 2 reject) → 返回 `_NEUTRAL_FALLBACK` (`tone="neutral"`, `narrative="数据已发布，详见上方数值。"`)。Priority 不因 LLM 失败漏掉 σ 升级——sigma 是 Python 算的。

### 推送优先级 P0/P1/P2/P3

每个推送在 emit 时由 `v2/reporting/priority.py` 计算 `importance_score`（0–100），映射为 4 个 tier：

| Tier | Score | 行为 | 示例 |
|---|---|---|---|
| **P0** | ≥ 80 | 立即推 + 🚨🚨🚨 前缀 + Dashboard 红框 | 价格 alert 触发、组合大额亏损、FOMC 决议、ARK 清仓持仓股、重大 8-K |
| **P1** | 60–79 | 立即推（无前缀） | 异动归因、watchlist 财报、13F 变动 |
| **P2** | 40–59 | 入 archive，16:45 ET 汇总一条 | 日筛选、产业链候选、ETF 静态快照 |
| **P3** | < 40 | 仅 archive，Dashboard 默认隐藏 | 弱归因异动、scheduler 心跳 |

**打分规则**：每类事件有基础分（见 `BASE_SCORES`），metadata 按规则调整：

| 场景 | 调整 |
|---|---|
| 异动归因没有 Tavily reasons | **-25**（降到 P3） |
| 异动归因有 ≥ 3 条 reasons | +10 |
| 含 `contrarian_move` flag | +15 |
| 价格变动 ≥ 5% | +10 |
| 该 ticker 在用户持仓 | **+15** |
| 该 ticker 在 watchlist（且非持仓） | +10 |
| 财报 surprise ≥ 10% | +15 |
| 当日组合亏损 ≥ 5% | **+30**（升 P0） |
| 当日组合亏损 2–5% | +10 |
| ARK 清仓 + 触及持仓股 | +10 + 15 |
| 8-K Item 2.x（业绩） | +5 |
| 8-K Item 5.x（高管变动） | +5 |
| 10-Q has_going_concern | **+20**（升 P0） |
| 10-Q has_material_weakness | +15 |
| ARK multi-fund 协同 | +15 |
| ARK large_new_position (≥2%) | +10 |

最终 score 在 0–100 之间 clamp，按 80/60/40 阈值映射到 P0–P3。

**工作流**：

```
agent 调 notifier.send_text(text, priority=...)
        ↓
TelegramNotifier 根据 tier 选行为：
  ├─ P0/P1: 立即推 Telegram（P0 加 🚨🚨🚨）+ archive 写入
  ├─ P2:    仅 archive；入 p2_digest_pending 表
  └─ P3:    仅 archive
        ↓
Dashboard 自动推送 feed：
  ├─ P0 顶置 + 红色 left border + 红色 P0 chip
  ├─ P1 蓝色 chip
  ├─ P2 黄色 chip
  └─ P3 默认隐藏；header toggle「📋 显示低优先级 (P3)」开启后才显示
        ↓
P2 汇总 cron（周一-周五 16:45 ET）：
  扫 p2_digest_pending → 拼一条「📋 今日 P2 汇总 · N 条」消息（P1 推送）→ 清队列
```

**实现注释**：纯 Python 规则，本地即时算出，不参与 LLM；notifier.send_text 不传 priority 时默认 P1（向后兼容）；archive 表通过 PRAGMA table_info 幂等迁移 priority 三列。

---

## 功能详解

三大功能模块的深入说明：**Cron Agents**（21 个调度任务按 family 组织，每个 family 自带数据源 / 触发逻辑 / Priority Ladder / 输出示例 / Bot 接口）+ **Streamer 盘中**（A 用户提醒 + B 自动扫描）+ **Telegram 交互界面**（slash 命令 / NL 示例 / Manager 别名详解）。

### 早期 5 个盘后 Agent

#### ① 基本面筛选（周一-周五 17:30 ET）

针对 TECH_30 universe 做硬规则筛选（市值、营收增速、毛利率、波动率），通过的候选打上 9 个定性标签（高成长 / 高毛利 / 小盘 / 业绩超预期 等）。最终给每只通过的标的生成"模板填充式"LLM 叙述——**模型只输出定性逻辑，所有数字由 Python 注入**，从源头消除数字幻觉。每个候选同时附加**板块相对强度对比**，把单股指标看不到的"领涨 / 掉队 / 同步"格局展现出来。通过的候选还会拉取近 7 日 Tavily 新闻标题 + 1 周回报相对同业中位数的差值。

#### ② 异动监测（周一-周五 17:35 ET）

在 TECH_30 universe 上检测三类异动信号：成交量 spike（≥3× 30 日均值）、52 周新高 / 新低、内部人大额买卖簇。每个触发的异动：

1. 自动加上**板块相对强度 chip**（当 ticker 与板块反向移动且差距 ≥1.5pp 时打 `★ 逆势`）
2. 进入多源归因管线：Tavily 新闻搜索 → 实体过滤 → Verifier LLM tier 评分 → Generator LLM 仅用 Tier 1 + Tier 2 来源做归因合成
3. 结果用确定性 ID `{ticker}_{date}` 持久化到 ChromaDB，供 RAG 检索

#### ③ 产业链横向扩展（周一 18:00 ET）

把前一日异动的 ticker 当作种子，让 DeepSeek 提议上下游邻居（供应商 / 客户 / 同业小盘 / 受益方）。每条提议的关系再经过一次 Tavily 搜索验证——**要求种子和邻居 ticker 在同一新闻中共现**，否则该关系被丢弃。通过验证的邻居再做市值 / 营收增速 / 毛利率门槛筛选才会被推送。

#### ④ 机构 13F（周二/周五 18:00 ET） + ④b 周度回填（周日 18:30 ET）

从 SEC EDGAR 拉取 10 位明星 manager 的最新 13F-HR：Berkshire（巴菲特）、Scion（Burry）、Pershing Square（Ackman）、Greenlight（Einhorn）、Renaissance、Two Sigma、D.E. Shaw、Citadel、Coatue、ARK（Cathie Wood）。对每个新 filing 计算季度对比的仓位变动，分类为"新进 / 加仓 / 减仓 / 清仓"，前 20 笔变动喂给 DeepSeek 做十字以内的策略性解读。**当前追踪 $1.29 万亿 AUM × 17,329 个仓位**。

周度回填（④b）每周做一次安全网——重新抓取所有 10 个 manager 的最新两个 filing，幂等地用 CUSIP-aggregated 数据覆盖 DB。捕获修正 filing、EDGAR 偶发错误、以及 Tue/Fri 推送 agent 可能漏掉的竞态。

#### ⑤ ETF 每日持仓（周一-周五 17:00 ET）

静默地从 ARK Invest 公开 CDN 抓取 4 只基金（ARKK / ARKW / ARKG / ARKF）的当日完整持仓 CSV，构建 (基金, 日期, ticker) 时间序列，实现 **24 小时调仓检测**——粒度比 45 天延迟的 13F 高了两个数量级。也是 ⑬ ARK Alerts 的底层数据源。

---

### Earnings Agent (⑦⑧)

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑦ Earnings Reminders | Mon-Fri **08:00 ET** | 检查 watchlist + 持仓 的 D-3/D-1/D-0 提醒 | D-3 = P2，D-1/D-0 = P1 |
| ⑧ Earnings Summaries | Mon-Fri **21:00 ET** | 当日发布的财报数据落地后 LLM 总结 + 反查 200 天内最新 10-Q 做 MD&A diff，否则标 pending 次日重试 | 基础 P1（70），\|surprise\| ≥ 10% 升 P0（+30）；going_concern +20 / material_weakness +15 |

**数据源**：

| 用途 | 源 | 备注 |
|---|---|---|
| 未来财报日历 | yfinance `Ticker.calendar` | FD 没有 forward calendar，必须 yfinance |
| 上次实际 vs 预期 | FD `get_earnings` | source-of-truth，按订阅计费 |
| 历史 surprise 趋势 | FD `get_earnings_history` | 最近 4 季用于 streak 展示 |
| Transcript URL | Tavily 搜索 | best-effort，可空 |
| 10-Q MD&A + Risk Factors diff | SEC EDGAR (edgartools) | ⑧ 反查 200 天内最近 10-Q + 与上一季对比 |

**10-Q MD&A 集成（⑧ 增强）**：每次推送总结卡时同步反查该 ticker 过去 200 天的最新 10-Q，与上一季 10-Q 对比 MD&A（Part I Item 2）+ Risk Factors（Part II Item 1A）差异。卡片末尾追加 "📋 10-Q MD&A 关键变化" 区块：**新增段落最多 3 条，每条 cap 80 字 + "…" 截断后缀**；新 risk factor heading 数量；conservative auditor flags（"going concern" / "material weakness" 正则命中）。10-Q 抓取失败 → 区块**静默省略**，earnings 卡照常推送。conservative auditor flags 直通 priority 系统：`has_going_concern` +20（足以升 P0），`has_material_weakness` +15。

**输出示例（BEAT 持仓股 + 15% surprise + 10-Q section，P0）**：

```
🚨🚨🚨 🟢 财报发布 · AAPL · BEAT
报告期：2026-06-30 · 申报：2026-08-01
🟢 持仓股

EPS：2.10 vs 预期 1.80 (+16.7%)
营收：$95.00B vs 预期 $91.00B (+4.4%)
最近 4 季：BEAT → BEAT → MISS → BEAT

👍 连续 BEAT，Services 加速
👎 iPhone 出货指引偏保守

本季强劲，但管理层语调偏谨慎

📜 电话会记录

📋 10-Q MD&A 关键变化
  ➕ Revenue growth driven by Services and wearables segments outpacing…
  ➕ Operating margin compressed by component cost inflation in line with…
  📌 1 个新 risk factor 段落
```

**Bot 接口（read-only，不写 archive 不算 priority）**：`/earnings AAPL`（单股财报卡）、`/earnings`（未来 14 天日历）、NL "苹果什么时候发财报" / "下周谁要发财报"。

**工程注释**：yfinance / FD 失败 silent skip；D-N 不缓存日期 → 自动吸收公司临时改 release 日；FD 数据当日没落地时推 P2 "数据待落地" 占位（archive 标 `pending_<today>`，次日 21:00 ET cron 自动重试）；Template-Fill 模式 LLM 只写定性 bull/bear/narrative，所有数字由 Python 注入；跨季度去重 `PRIMARY KEY (ticker, report_period)`，pending 行不进 `get_summarized_set()`。

---

### Portfolio Risk Agent (⑨⑨b⑩)

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑨ Portfolio Risk | Mon-Fri **18:30 ET** | 实时拉 RiskReport 全景（drawdown 含 today realtime fix）| 见 ladder |
| ⑨b Positions Snapshot | Mon-Fri **16:25 ET** | 静默后台：写当日持仓快照到 `positions_snapshot` 表 | 无推送（archive trace 仅） |
| ⑩ Portfolio Weekly | Fri **19:00 ET** | 周复盘 + 月回报 + 1M 回撤 + 行业暴露 + **per-position attribution** | 固定 P1（floor）|

**数据源**：

| 用途 | 源 | 备注 |
|---|---|---|
| 当前持仓 + 现金 | Alpaca `account` + `positions` | TOTAL = invested + cash，invested_value 是派生 |
| 1M equity 历史 | Alpaca `portfolio_history(1M, 1D)` | 算 1W / 1M 回报 + 峰-谷回撤 |
| 行业 ETF 映射 | `v2/universe/etfs.py` 90 个 ticker | OTHER 桶覆盖未映射 |
| 7 天财报风险 | 复用 `v2.earnings.calendar` | 不重复实现 yfinance 调用 |

**Priority Ladder（⑨ 专用）**：

| 触发条件 | base + adjustments | score | tier |
|---|---|---|---|
| 正常一天 | 55 | 55 | P2 |
| top_1 ≥ 20% 单独 | 55 + 10 | 65 | P1 |
| top_1 ≥ 30% 单独 | 55 + 20 | 75 | P1 |
| daily_pnl ≤ -2% 单独 | 55 + 10 | 65 | P1 |
| **daily_pnl ≤ -5% 单独** | 55 + 30 | 85 | **P0** |
| 1M 回撤 ≥ 10% 单独 | 55 + 15 | 70 | P1 |
| 未来 7 天 ≥ 3 只财报 | 55 + 10 | 65 | P1 |
| **多因子叠加 (top1+pnl+dd)** | 最多 +75 | 100 (capped) | **P0** |

**设计哲学**：单一异常因子最多升 P1（提醒，不打扰）；单日大跌或多因子叠加直接升 P0。

**输出示例（⑨ 多因子 → P0 卡）**：

```
💼 组合风险 · 2026-06-04
━━━━━━━━━━━━━━━━━━━━
组合价值 $128.6K (持仓 $103.2K · 现金 $25.4K, 19.8%)
今日 P/L 🔴 -1.78% (-$1.9K)
本周 -2.30% · 本月 -3.40%
📊 集中度
  Top 1: NVDA 35.0% ⚠️
  Top 5: 65.0%
  HHI: 0.18 (中等集中)
🏭 行业暴露
  SMH (半导体): 38.0% ⚠️
  XLK (科技): 30.0% ⚠️
  OTHER (其他): 17.0%
  XLF (金融): 15.0%
📉 回撤 (1M) 当前 -3.40% · 最大 -12.00% (峰值 $107.0K @ 2026-06-01)
📅 未来 7 天财报风险 (2 只)
  2026-06-07 AAPL (D-3)
  2026-06-08 NVDA (D-4)
⚠️ 单票 NVDA > 30% / SMH 行业 > 30% / 1M 回撤 > 10%
```

**输出示例（⑩ Clean Week + per-position attribution → P1 floor）**：

```
📊 周 P&L 复盘 · 2026-06-06
(截至昨日收盘的口径)
━━━━━━━━━━━━━━━━━━━━
组合价值 $130.0K (持仓 $104.6K · 现金 $25.4K, 19.5%)
本周回报 🟢 +1.50%
本月回报 🟢 +2.80%
📉 1M 最大回撤 -1.80%
  峰值 $105.1K @ 2026-06-03
  当前距峰 -0.50%
🏭 主要行业暴露
  XLK: 35.0%
  SMH: 30.0%
  XLF: 18.0%
📊 本周 per-position 表现归因
  最佳: NVDA 🟢 +5.00% (贡献 🟢 +1.50%)
  最差: JPM  🔴 -2.00% (贡献 🔴 -0.30%)
  净贡献: 🟢 +1.40%
```

**Attribution 3-state gating**：≥5 天 ⑨b snapshot 覆盖 → 最佳 / 最差 / 净贡献 三行（worst 行在单持仓周自动跳过避免重复）；1-4 天 → 显示 `<i>归因数据累积中 (N/5 天)</i>` 占位；0 天 → 完全静默（fresh-account contract，不为缺数据道歉）。

**Drawdown realtime fix**：Phase 2 时 `compute_drawdown` 只看 `get_portfolio_history()` 的 EOD canonical 序列（截止昨日收盘），今天的 intraday 下跌不进 series → 卡片矛盾显示 "今日 P/L -3.72% / drawdown 0.00%" (2026-06-05 prod 实测)。Fix: `compute_drawdown(broker, today_realtime_value=portfolio_value)` 把 Alpaca real-time portfolio_value append 到 EOD 序列（或 overwrite 今天点 idempotent on rerun），peak / max_dd / current_dd 自动包含今天 → 卡片即时反映真实下跌。Backward-compat 保留：`today_realtime_value=None` 行为同 Phase 2。

**Bot 接口**：`/risk`（实时 read-only）、`/pnl [day|week|month]`、NL "组合风险怎么样" / "这周亏了多少" / "本月赚了多少"。

**工程注释**：`portfolio_value` 是 TOTAL（Alpaca 原值 = invested + cash），`invested_value` 派生 @property 防漂移；weight 分母是 invested_total（cash 不参与集中度——"Top 1 NVDA 35%" 是直觉的"持仓里 35%"）；drawdown 5 层符号防御（compute 返回非负 → 内部 `assert ≥ 0` → renderer 强制 `-` 前缀 → 阈值用 raw 不加 abs → 调用方传 signed/unsigned 都行）；Alpaca 不可用 silent recovery 写 `RiskReport.warnings` + 仍推 P2 "数据不全" 卡。

---

### SEC 监控 (⑪⑫⑫b)

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑪ SEC 8-K | Mon-Fri **17:05 ET** | 单 ticker 当日 8-K → 单卡聚合所有 items | base by max item tier，5.02 senior_exec +15 |
| ⑫ SEC Form 4 | Mon-Fri **17:45 ET** | P/S 单笔卡 + 同日 ≥3 cluster 卡 | base 75 (P) / 50 (S) + magnitude / role / 10b5-1 调整 |
| ⑫b SEC Insider Weekly Digest | **Fri 19:15 ET** | 反查本周 ⑫ 推送 → 周报卡 | base 55 (P2)；unusual ≥3 tickers 升 P1 |

**数据源**：edgartools 5.31.5（SEC EDGAR API wrapper），200ms throttle；复用 Phase 1 的 `EDGAR_IDENTITY` env var；单独 `v2/sec/` 模块，与 `v2/institutional/`（13F 路径）解耦。

**⑪ 8-K Scanner — 24 个 Item Codes 优先级表**：

| Tier | Item codes |
|---|---|
| **P0** | `1.03` Bankruptcy · `1.05` Cybersecurity · `2.04` Triggering events on off-BS · `3.01` Notice of delisting · `4.02` Non-reliance on prior financials · `5.01` Change in control · `5.02` Senior exec departure（LLM 确认） |
| **P1** | `1.01` Material agreement · `1.02` Termination · `2.01` Acquisition / disposition · `2.03` Material obligation · `2.05` Restructuring · `2.06` Material impairment · `4.01` Change in auditor · `5.02` Other officer changes |
| **P2** | `3.02` Unregistered sales · `3.03` Material modification of rights · `5.08` Shareholder director nominations · `7.01` Reg FD · `8.01` Other events |
| **P3** | `1.04` Mine safety · `5.03` Bylaw amendment · `5.04` Suspension of blackout period · `5.05` Amendments to ethics code · `5.07` Submission to shareholder vote · `9.01` Financial exhibits |

**`2.02` Results of Operations 严格跳过** —— 该数据由 ⑧ Earnings Summaries 在 21:00 ET 处理。混合 2.02 + 其他 items 的 filing 保留卡片但 2.02 行注 "(⑧ 处理)"；纯 `{2.02}` 或 `{2.02, 9.01}` 的 filing 整张丢弃。

**多 item 单卡**：HPE 风格的一张 8-K 同时声明 1.01 + 2.02 + 5.02 + 7.01 + 9.01 时合并成一张卡，`max_priority_tier` 决定整条 priority。Stage 0 真实数据校准：分裂成 5 张卡会污染推送频道。

**5.02 LLM 抽取**：`v2/sec/ner_5_02.py` 用 DeepSeek template-fill 提取 `{departures, appointments, has_senior_exec}`。LLM 不出数字，只抽 entity（name + title）。LLM 失败时 `extracted_meta={}` —— 卡里显示 "(姓名待解析)" 占位（bot 路径）或省略抽取块（cron 路径），priority 不因 LLM 失败自动升级（保守语义）。

**⑫ Form 4 — Noise vs Signal**（Stage 0 真实数据校准 10 ticker × 30 天 = 89 transactions）：

| 类型 | Code | 占比 | 含义 | 处理 |
|---|---|---|---|---|
| Noise | A | 44% | Award（RSU / 股权激励） | `noise_summary` 聚合 |
| Noise | M | 21% | Exercise（option vest） | `noise_summary` 聚合 |
| Noise | F | 15% | Tax withhold（RSU vest 税款） | `noise_summary` 聚合 |
| Noise | G | 3%  | Gift（赠予） | `noise_summary` 聚合 |
| Noise | C | <1% | Conversion（衍生品转换） | `noise_summary` 聚合 |
| **Signal** | **P** | 8%  | **Purchase（自掏腰包买入）** | **单卡推送** |
| **Signal** | **S** | 8%  | **Sale（卖出）** | **单卡推送** |

83% 的 Form 4 是 noise —— 这些不是信号。只有 P / S 走单卡推送路径。

**Cluster 检测**：同 ticker 同日同方向（purchase / sale）的 P/S 交易，**distinct insider 数 ≥ 3** → 一张 cluster 卡列出所有人 + 总 USD。Stage 0 早期版本用 30 天滚动窗口 → 80% universe 都触发；同日同方向才是真正的协同事件，巧合在 ≥3 个 insider 上极难。

**⑫b 周报（title-only 简化口径）**：每周五 19:15 ET 把本周（Mon-Fri）所有 ⑫ Form 4 推送从 `archive.pushes` 反查、按 (ticker, direction) 聚合成一张周报卡片。时段卡在 ⑩ Portfolio Weekly（19:00）和 ⑰ Macro Weekly（19:30）之间。Phase 3.5 Stage 0 trace 探测确认 ⑫ archive `trace_json` 只持久化"N signal · Y cluster · Z noise"聚合摘要 —— per-transaction A/M/F/G/C breakdown 没有落库。为避免扩展 archive schema，⑫b **纯走 title-only fallback**：正则解析 `Form 4 · NVDA · 买入` 与 `Form 4 集群 · ARM · purchase` 两种 title shape，聚合至 (ticker, direction) 粒度。卡片 footer 显式注明这是简化口径，per-code 完整 breakdown defer 到 **Phase 3.5.5**。Priority P2 floor 同 ⑩⑰ —— operator visibility 第一；≥3 个 ticker 一周内被 push ≥3 次升 P1。空周仍推一张 "本周 ⑫ 推送平静" 卡片保证 operator 看到 cron 跑了。

**Priority Ladder**：

⑪ 8-K base scores: `sec_8k_p0=85` / `sec_8k_p1=65` / `sec_8k_p2=55` / `sec_8k_p3=35`。调整：5.02 LLM 确认 senior_exec（P0 only）+5；8-K/A 修正版 -5；持仓股 +15；watchlist +10。

⑫ Form 4 base scores: `sec_form4_purchase=75` / `sec_form4_sale=50` / `sec_form4_cluster=75`。调整：Purchase ≥$1M +25 / $100K-$1M +10；Purchase by CEO/CFO +10；Purchase 10b5-1 plan -10；Sale ≥$10M discretionary +15；Sale ≥$1M 10b5-1 plan -5；Cluster ≥5 distinct +15 / 3-4 distinct +5；Cluster purchase 方向 +10；持仓股 / watchlist +15 / +10。

**设计哲学**：discretionary sale 比 10b5-1 plan sale 信号强（plan 是几个月前 pre-arranged），cluster purchase 比 cluster sale 信号强（卖股原因多样，集体买入难以解释为巧合）。

**输出示例**：

⑪ HPE 多 item P0 卡：

```
🚨 SEC 8-K · HPE · P0
申报日：2026-06-04
🟢 持仓股

项目
  📋 1.01 [P1] 重大商业合约 (新签)
  📎 2.02 [P2] 财报数据  (数据由 ⑧ 处理)
  🚨 5.02 [P0] 高管 / 董事会变动
  📎 7.01 [P2] Reg FD 自愿披露
  📌 9.01 [P3] 财务报表 / 附件

5.02 抽取
  📤 离职：John Smith (Chief Executive Officer)
  📥 任命：Jane Doe (Interim Chief Executive Officer)
```

⑫ Jensen $2.5M CEO 买入 P0 卡：

```
📥 内部人买入 · NVDA
申报：2026-06-04 · 交易：2026-06-04
🟢 持仓股

申报人：Jen-Hsun Huang · 🔴 CEO
交易：20,000 股 × $125/股 = $2.50M (discretionary)
```

⑫b 内部人活动周报（unusual 状态 → P1）：

```
📥 内部人活动周报 · 2026-06-01 → 2026-06-05
━━━━━━━━━━━━━━━━━━━━
本周总览
  总 push 数: 15 · 涉及 ticker: 5 只

方向分布
  📥 买入 (purchase): 4 笔
  📤 卖出 (sale): 8 笔
  🔗 集群 (cluster): 3 笔 (买入 2 / 卖出 1)

⚠️ 异常活跃 ticker (≥3 pushes)
  NVDA: 5 pushes
  ARM:  4 pushes
  TSLA: 3 pushes

注：基于 ⑫ push title 统计（per-code A/M/F/G/C breakdown 见 Phase 3.5.5）
```

**Bot 接口**：`/8k AAPL`、`/insiders NVDA [days]`（bounded 7-365）、NL "AAPL 最近 8-K" / "NVDA 内部人交易" / "NVDA 过去 30 天 insider"。

**工程注释**：edgartools.Filing.obj() shape 校准（PascalCase `Code` 列名不是 dataclass field 名 `transaction_code`，Stage 0 实跑回归）；5.02 senior-exec 升级保守语义（LLM 失败 → 不自动升 P0）；2.02 严格 skip vs 混合保留（`EightKEvent.is_2_02_only` 判断 `material_codes == {2.02}`）；10b5-1 plan 检测（footnote 正则匹配，约 1/3 大额 sale 是 plan）；5 + 1 个 SEC formatter 公共 API source-of-truth 在 `v2/sec/_bot_cards.py`（sandbox-runnable）。

---

### Macro Agent (⑭⑮⑯⑰)

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑭ Daily Snapshot | Mon-Fri **16:30 ET** | VIX/yields/大宗实时 + 4 个 anomaly flag | 见 ladder |
| ⑮ Release Scanner | Mon-Fri **09:00 ET** | release_calendar 命中日的 CPI/PCE/NFP/GDP/PPI/FOMC | σ ladder |
| ⑯ Initial Claims | **Thu 09:30 ET** | 周度 ICSA + 4W MA smoothed level | base P2, σ>2 升 P1 |
| ⑰ Macro Weekly | **Fri 19:30 ET** | 本周已发布 + 下周预告 + 周内 VIX/yields 变化 | 固定 P1 (floor) |

**数据源 — hybrid 策略**（Layer 优先级 + 单点失败 silent skip per field）：

| 用途 | 源 | 备注 |
|---|---|---|
| Treasury yields (2Y/10Y/T10Y2Y/T10Y3M) + Fed Funds Upper/Lower | **FRED** canonical EOD | 避开 yfinance `^TNX` / `^FVX` / `^TYX` 返回 × 10 raw 值的坑 |
| CPI/PCE/NFP/GDP/PPI/Claims series | FRED canonical | 含 vintage_dates 处理 prelim → final 修正 |
| VIX 实时 + DXY + WTI + Gold | **yfinance** (15min 延迟) | EOD canonical 由 FRED `VIXCLS` 兜底 |
| FOMC sell-side aggregate | **Tavily** (8 trusted domains) | 替代 LLM 鹰鸽判断（Layer 3 防御）|
| Release 解读 bull/bear/narrative/tone | **DeepSeek** template-fill (Layer 1+2) | 数字全 Python，LLM 只出 label |

**实现细节**：`fredapi.Fred.get_series` 走 fredapi wrapper；`get_release_dates` fredapi 不支持，直接 httpx 调 `https://api.stlouisfed.org/fred/release/dates` REST endpoint（Stage 1 prod-box 跑 `_seed_calendar` 时发现的 bug，patch 用 3 次 backoff 重试）。

**release_calendar.py · 硬编码 2026 schedule**：cron 路径必须 deterministic + 离线可跑，BLS/BEA 的官方 schedule 一年只变几次。`_seed_calendar.py` 一次性脚本用 FRED `/release/dates` REST API 生成 40 个 release entries 覆盖 33 个 dates（CPI/PCE/NFP/GDP/PPI 各 7 + FOMC × 5）粘贴到 release_calendar.py 的 `AUTOGEN BLOCK START/END` sentinel 之间。`_LAST_UPDATED` 字段 + 6 个月 staleness check 在 cron 启动时 warn——operator 看到 warning 重跑 `_seed_calendar.py` 更新 dict 即可。Claims (ICSA) 不进 calendar：周度 deterministic Thursday 触发，⑯ cron 用 `CronTrigger(day_of_week="thu")` gate 直接跑 `build_claims_event`，不查 calendar。

**⑭ Daily Snapshot — Anomaly 升级**（Base: `macro_snapshot_p3=35` / `macro_vix_spike=85` / `macro_curve_flip=65`）：

| 触发条件 | 调整 | kind |
|---|---|---|
| VIX 单日 ≥ +30% (extreme) | +20 | `macro_vix_spike` (P0) |
| VIX 单日 ≥ +20% (strong) | +10 | `macro_vix_spike` (P0) |
| T10Y2Y 由正转负今日翻转 | +10 (yield_curve_inverted) | `macro_curve_flip` (P1) |
| VIX 单日 ≥ +10% 偏高 OR DGS10 单日 \|Δ\| ≥ 20bps shock | 经 `macro_curve_flip` 路由 | P1 |
| 正常日（无 anomaly） | 仅 base 35 | `macro_snapshot_p3` (P3, 日常 ambient 推送) |

**⑮ Release Scanner — σ ladder**（Base: `macro_release_p2=55` / `macro_release_p1=65` / `macro_release_p0=85`）：

| 条件 | 调整 | tier |
|---|---|---|
| `\|surprise_sigma\|` ≥ 3.0 extreme | +20 | P0 (`macro_release_p0`) |
| `\|surprise_sigma\|` ≥ 2.0 big | +10 | P1 (`macro_release_p1`) |
| `\|surprise_sigma\|` ≥ 1.0 moderate | +5 | P1 nudge |
| FOMC + SEP `hawkish_shift` / `dovish_shift` | +15 (`sep_*_shift`) | P0 boost |
| Tavily 卖方读数 hawkish_unexpected | +10 (`sell_side_hawkish`) | P0/P1 nudge |

**FOMC 路径**：σ ladder 不适用（FOMC 没有"surprise σ"——市场 priced-in 程度太杂）。`_fomc_kind_and_meta` 看 `event.sep_dot_plot_change`：`hawkish_shift` / `dovish_shift` 升 `macro_release_p0`；`no_change` 走 `macro_release_p1` base 65（FOMC 永远至少 P1）。

**⑯ Initial Claims** ICSA 每周四 08:30 ET 发布，cron 09:30 ET 跑（给 FRED 1 小时缓冲）。Base `macro_release_p2=55` (P2)，`|σ| ≥ 2` 升 `macro_release_p1=65` → P1。假期周（Thanksgiving / 跨年）FRED 没数据 → silent skip + log INFO。

**⑰ Weekly Recap**：固定 P1 floor 同 ⑩ Portfolio Weekly 哲学——周报 operator 必须看见，不靠事件驱动。19:30 ET 错开 ⑨ 18:30 + ⑩ 19:00，与 ⑧ 21:00 也有 90 分钟缓冲。卡片含本周 already-fired releases + 下周 schedule preview + 周内 4 个核心 series 变化（VIX 用 pts 单位，DGS10/DGS2/T10Y2Y 用 bps 单位 —— industry standard）。

**Bot 接口**：`/macro`（综合 dashboard）、`/cpi`（最新 CPI release 卡）、`/fomc`（最近 FOMC + 下次预告）、`/yields`（复用 macro_view yields 区块）、NL "宏观怎么样" / "最近 CPI 数据" / "上次 FOMC 怎么说" / "NFP data this month"。

**工程注释**：fredapi REST patch（`_seed_calendar.py` 暴露 `'Fred' object has no attribute 'get_release_dates'` bug → httpx 直接调 FRED REST endpoint，3 次 1s linear-backoff retry）；6 层 macro formatter 公共 API source-of-truth 在 `v2/macro/_bot_cards.py`（sandbox-runnable）；Layer 3 FOMC 路径 Python only（fomc_parser.py 对比 15 个 KEY_PHRASES 的 added/removed，extract_dot_plot_table 用 regex 提取 Federal funds rate 行 + 年份头，Tavily 走 8 trusted domains 做简单 keyword count majority vote，**LLM 完全不参与鹰鸽判断**）。

---

### ARK 调仓告警 (⑬)

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑬ ARK Alerts | Mon-Fri **08:30 ET** pre-market | 读 ⑤ 已写入 `etf.db` 的昨日 baseline + 今日 CSV → diff → 显著调仓 | base 65 (P1)；多 fund 协同 / 持仓股 / 大仓位升 P0 |

**背景**：ARK Invest 是 Cathie Wood 主动管理的 ETF 家族，与传统 ETF 不同——**每个交易日公开当日 holdings**。Cathie 的 conviction trades 比 13F 季度披露提前 **45 天看到**：新建仓 / 清仓 / 显著调仓都是 same-day 信号。突然清仓某 ticker = strong negative；新建仓 +0.5% 起 = strong positive（尤其 small-cap）；多 fund 同向 = 部门级 conviction。

**数据源 — 复用 ⑤ ETF 基础设施**：v2/etf/ 模块自 Phase 0 起就在跑 ARK CSV 抓取 + 日 diff + SQLite 持久化（⑤ ETF Daily Snapshot 17:00 ET cron）。⑬ 复用整套基础设施，只新增 alerts 分类层 + 卡片 formatter，**零 schema 改动、零 client/detector/tracker 改动**。`SUPPORTED_FUNDS = ["ARKK", "ARKW", "ARKG", "ARKF"]`（ARKQ + ARKX 由 ARK 在 assets.ark-funds.com 已 deprecate，无 CSV 暴露）。

**4 类 action + 阈值**（`v2/etf/alerts.py` 唯一 source of truth）：

| Action | 阈值 |
|---|---|
| `new_position` | today_weight ≥ 0.5% |
| `liquidated` | yesterday_weight ≥ 0.5% |
| `increase` | `|relative_change|` ≥ 20% |
| `decrease` | `|relative_change|` ≥ 20% |

**Priority 升级**（base `ark_alert_p1` = 65, P1）：

| 触发条件 | 调整 | reason 标签 |
|---|---|---|
| User universe（held ∪ watchlist）| +10 | `held_or_watchlist_ark` |
| Multi-fund 协同（≥2 funds same dir same ticker）| +15 | `multi_fund_coordination` |
| Large new position（today_weight ≥ 2%）| +10 | `large_new_position_X.X%` |
| Large liquidation（yesterday_weight ≥ 2%）| +10 | `large_liquidation_X.X%` |

**设计哲学**：multi-fund 协同（不同 PM 同日同向）是最强 conviction 信号 → 单独 +15。User universe boost 用独立 reason 名（`held_or_watchlist_ark` 不与通用 `held_position` 共用）便于审计 trail 区分 cron 来源。

**Multi-fund 协同检测**：`v2/etf/alerts.py:_mark_multi_fund` 在 per-fund 分类完成后做 cross-fund 聚合：按 `(ticker, direction)` 分组（direction = `"buy"` if action ∈ {new_position, increase} else `"sell"`），≥2 funds 同向同 ticker → 所有匹配 alert mark `is_multi_fund=True`。**反向同 ticker（一买一卖）显式 NOT 协同**——这是不同 fund manager 的独立判断，不是 conviction signal，regression 钉死。

**卡片 UX**：4 action 各自模板（🟢 新建仓 / 🔴 清仓 / 📈 增持 / 📉 减持），加 2 个可选 banner（顺序：multi-fund 优先 → user-universe 次之 → divider）。总览卡含触发 alerts 总数 + 多 fund 计数 + 涉及持仓 / 关注计数 + 行动分布（buys cluster 新建仓+增持 → sells cluster 清仓+减持，认知分组）+ 涉及 user universe ticker 列表 + 多 fund 协同 ticker 与 fund 详情 + **本日扫描 funds: (N/M ARK funds)** 分数（partial-failure 透明度，e.g. ARKG 503 → "(3/4 ARK funds)" + warnings 行）。

**输出示例**（Multi-fund 协同 + 持仓股 P0 卡，base 65 + held +10 + multi_fund +15 = 90）：

```
📈 ARK 增持 · TSMC
Fund: ARKK · 今日权重: 3.50% (+30.0%)
🚨 多 Fund 协同（详见总览）
🟢 持仓股 / 关注列表
━━━━━━━━━━━━━━━━━━━━
增持: 100,000 shares · ≈ $15.0M
昨日权重: 2.30%
```

Summary 总览卡（6 alerts mixed）：

```
🔔 ARK 调仓总览 · 2026-06-09
━━━━━━━━━━━━━━━━━━━━
本日触发 alerts: 6
  🚨 多 Fund 协同: 2
  🟢 涉及持仓 / 关注: 4

行动分布
  🟢 新建仓: 1
  📈 增持: 3
  🔴 清仓: 1
  📉 减持: 1

涉及 user universe: NVDA · TSLA · TSMC (3 个)

多 Fund 协同
  TSMC: ARKK + ARKQ

本日扫描 funds: ARKK / ARKW / ARKG / ARKF (4/4 ARK funds)
```

**First-deploy edge** 显式处理：`get_latest_snapshot_before` 返回 None → silent skip per fund（⑤ 17:00 ET 后续 populate baseline，次日开始有 signal）。

---

### Streamer 盘中

与 scheduler 并行运行。交易时段（周一-周五 9:30–16:00 ET）每 60 秒轮询一次，非交易时段 5 分钟一查。

#### A. 用户设置的价格提醒

用户通过 `/alert NVDA 130 above` 或自然语言"提醒我 NVDA 突破 130"创建提醒。Streamer 每分钟：

1. 查 `alerts` 表里所有未触发的条目
2. 把涉及的 ticker 批量打包成一次 Alpaca `get_stock_latest_trade` 请求
3. 对每个 ticker 跑 `alert_fire_check(ticker, current_price)` —— SQL 原子性地标记任何已突破的提醒（`UPDATE … WHERE fired_at IS NULL`）并返回
4. 每个触发的提醒推送一张卡片；永远不重复触发（设计上的 one-shot 语义）

#### B. TECH_30 自动异动扫描

每分钟同时扫描全部 29 只 TECH_30 ticker 寻找盘中异动：

- **双门槛**：开盘以来 ≥3% 价格变动 **AND** ≥2.5× 成交量节奏
- **成交量节奏** = `今日成交量 / (30 日均值 × 时段进度)` —— 对部分交易时段做归一化，避免开盘第一小时的 false positive
- **30 日均值 baseline** 每 7 个交易日自动刷新（通过 Alpaca daily bars）
- **板块相对强度** 自动附带（`★ 逆势` chip 当反向移动时出现）
- **30 分钟单 ticker 冷却期** 通过 `intraday_cooldown` 表防止刷屏
- **盘中刻意不调 LLM** —— 信号快速浮现是目标，深度归因留给 17:35 ET 盘后 agent

典型触发卡片：

```
⚡ 盘中异动 · IBM · 10:15 ET
━━━━━━━━━━━━━━━━━━━━
📈 现价 $297.97  +7.45%  vs 开盘 $277.30
当日成交 28.4M  ·  节奏 3.6×  (已交易 11% 时段)
对比 XLK +0.40%  ·  差 ↑+7.05pp ★ 逆势

用 /why IBM 看盘后完整归因（17:35 ET 起效）
```

---

### Telegram 交互界面

机器人的自然语言层把任意中文 / 英文文本归类到 **23 个固定意图**之一，分类器是 DeepSeek temperature=0 + JSON 输出 + 白名单枚举校验。**任何不在白名单的输出都 fallback 到 `unknown`**，保证行为有界。

#### Slash 命令完整列表

| 类别 | 命令 | 功能 |
|---|---|---|
| Watchlist | `/watchlist`, `/add NVDA`, `/remove TSLA` | 管理个人关注列表 |
| 分析 | `/why TICKER` | 按需做异动归因 |
| | `/summary TICKER` | 多维度快照（价格 + 财务 + 财报 surprise + 内部人 + 新闻）|
| | `/chain TICKER` | 产业链横向扩展 |
| | `/13f MANAGER` | 完整组合 + 季度变动 |
| | `/holders TICKER` | 反查：哪些 manager 持有该 ticker |
| | `/etf SYMBOL` | ARK 基金当日持仓 + 24h 调仓 |
| | `/earnings AAPL` | 单股财报卡（下次日期 + 上次结果 + ⭐ 标记） |
| | `/earnings` | 未来 14 天财报日历（watchlist ∪ 持仓） |
| 提醒 | `/alert NVDA 130 above` | 创建价格门槛提醒 |
| | `/alerts` | 列出未触发的提醒 |
| | `/alert_remove ID` | 删除某条提醒 |
| 账户 | `/portfolio` | Alpaca 当前持仓 + 现金 |
| | `/pnl [day\|week\|month]` | 盈亏（默认 day；week/month 走 ⑨⑩ 数据）|
| | `/risk` | 实时组合风险快照（同 ⑨ cron，read-only）|
| SEC | `/8k TICKER` | 过去 30 天 8-K 摘要 + 5.02 LLM 抽取（同 ⑪ cron 数据源） |
| | `/insiders TICKER [days]` | 过去 N 天 Form 4 摘要（默认 90，bounded 7-365）|
| Macro | `/macro` | 综合宏观 dashboard（VIX / yields / 最近 + 下次 release）|
| | `/cpi` | 最新 CPI release 卡（含 surprise σ + summarizer 输出）|
| | `/fomc` | 最近 FOMC 决议（statement diff + SEP + Tavily 卖方读数）+ 下次会议预告 |
| | `/yields` | 收益率曲线（复用 `/macro` dashboard）|
| 元信息 | `/settings`, `/help`, `/start` | 阈值查看 + 命令参考 |

#### 自然语言示例（23 NL intents）

| 用户输入 | 路由到 |
|---|---|
| `NVDA 为什么跌？` | `explain_move` · NVDA |
| `看看 AAPL 怎么样` | `summary` · AAPL |
| `找一下 AMD 的产业链` | `chain` · AMD |
| `巴菲特最近买了什么` | `thirteen_f` · brk |
| `谁持有 NVDA` | `holders_view` · NVDA |
| `Cathie 今天买啥` | `etf_view` · ARKK |
| `提醒我 NVDA 突破 130` | `alert_set` · NVDA · 130 · above |
| `我设了哪些提醒` | `alert_list` |
| `看看 Alpaca 持仓` | `portfolio_view` |
| `我的当日盈亏` | `pnl_view` |
| `我关注了哪些股票` | `watchlist_view` |
| `最近有什么异动` | `find_anomalies` |
| `苹果什么时候发财报` | `earnings_view` · AAPL |
| `下周谁要发财报` | `earnings_calendar` · days_horizon=7 |
| `组合风险怎么样` | `risk_view` |
| `这周亏了多少` | `pnl_period` · period=week |
| `本月赚了多少` | `pnl_period` · period=month |
| `AAPL 最近 8-K` | `eight_k_view` · AAPL |
| `NVDA 内部人交易` | `insider_view` · NVDA |
| `NVDA 过去 30 天 insider` | `insider_view` · NVDA · days_back=30 |
| `宏观怎么样` | `macro_view` |
| `最近 CPI 数据` | `release_check` · release_type=cpi |
| `上次 FOMC 怎么说` | `release_check` · release_type=fomc |
| `NFP data this month` | `release_check` · release_type=nfp |
| `今天天气怎么样` | `unknown` |

#### Manager 别名（支持 10 位）

`brk / berkshire / buffett`、`burry / scion`、`ackman / pershing`、`einhorn / greenlight`、`renaissance / rentech`、`twosigma`、`deshaw / shaw`、`citadel`、`coatue`、`ark / cathie / wood`

---

## 板块相对强度对比 · 数据源

### 板块相对强度对比

每条信号（筛选 / 盘后异动 / 盘中异动）在被推送之前都和板块 ETF 做相对强度对比。`NVDA +5% 量爆`这条信号在 `SMH +4.5%` 时几乎是 beta，在 `SMH -1%` 时才是真正的 ticker-specific catalyst。

| Ticker 类别 | 对应板块 ETF |
|---|---|
| 半导体（NVDA, AMD, AVGO, QCOM, INTC, TXN, MU）| SMH |
| 大盘科技 + 软件 + 互联网（AAPL, MSFT, GOOGL, META, ORCL, CRM, ADBE, …）| XLK |
| 默认兜底 | SPY |

板块 ETF 的价格序列在每次 agent 启动时一次性预取（全 universe 加 3 个额外 FD 调用）。`contrarian` flag 仅在 ticker 与板块**反向**且差距 ≥1.5pp 时触发——只有此时 `★ 逆势` chip 才出现。

**生产环境真实案例**：`MU +3.63%、SMH -1.10%、1.5× 量 → ★ 逆势 chip + Tier-1 归因独立 surface「MU 市值突破 $1T」作为 ticker-specific catalyst`。

### 外部数据源（7 个）

| 数据源 | 提供什么 | 为什么选这个 |
|---|---|---|
| **yfinance** | Daily OHLCV（默认 daily prices）+ 期权链 + macro VIX/DXY/WTI/Gold | 实时 EOD（无 FD free tier 1-3 天滞后）；免费；驱动 ① 筛选 / ② 异动 / ③ 横向扩展 / `/why` `/summary` / macro snapshot |
| **financialdatasets.ai** | 财报历史 / 内部人交易 / 公司基本面 | 季度 / 离散事件型数据无每日 lag；主商业 API；schema 一致 |
| **SEC EDGAR**（edgartools）| 13F-HR 机构持仓 + 8-K / Form 4 / 10-Q 监控 | 权威源 |
| **ARK Invest CDN** | ETF 每日持仓 CSV | 公开免费；日级别粒度 |
| **Alpaca**（paper）| 实时价格（streamer）、账户持仓 + P&L | 免费；驱动 `/portfolio` / `/pnl` / streamer alerts / portfolio risk cron |
| **FRED** | 宏观时间序列（CPI/PCE/NFP/GDP/PPI/Treasury yields/Fed Funds）| 权威 EOD 源；含 vintage 处理；避开 yfinance `^TNX` × 10 raw bug |
| **Tavily News API** | 归因 + 验证用的新闻搜索 + FOMC sell-side aggregate | 搜索引擎级别质量；Layer 3 防御替代 LLM 鹰鸽判断 |

LLM 层：**DeepSeek** (`deepseek-chat`，中英双语性价比最高) 做 generation + verification + intent classification；**OpenAI** `text-embedding-3-small` 做 RAG embeddings。

**价格源解耦**：`v2/data/price_source.py` 暴露 `PriceSource` Protocol + `YFinancePriceSource`（默认）+ `FDPriceSource`（backtest / event-study 仍用，保 reproducibility）。Ops 通过 `V2_PRICE_SOURCE=fd` env var 可一键 fallback 回 FD（无需重新部署）。FD 客户端仍 source-of-truth 处理非时间敏感数据。

---

## 项目结构 · 技术栈

**技术栈**：Python 3.11 + Poetry；LangChain + `langchain-deepseek`；APScheduler `BlockingScheduler` + `US/Eastern` cron；python-telegram-bot（异步、polling）；alpaca-py；SQLite WAL；ChromaDB；edgartools；matplotlib + Noto Sans CJK；systemd（Ubuntu 24.04）。

```
v2/
├── data/             # FDClient + CachedFDClient + NewsProvider Protocol + price_source.py
├── screening/        # ① 基本面筛选 + delta 增强 + 叙述器
├── monitoring/       # ② 检测器 + 多源归因 + Verifier
├── lateral/          # ③ LLM 扩展 + Tavily 关系验证
├── institutional/    # ④ EDGAR 客户端 + CUSIP 聚合 + 变动
├── etf/              # ⑤⑬ ARK CSV 客户端 + snapshot + diff + alerts 分类层
├── earnings/         # ⑦⑧ yfinance 日历 + FD 历史 + LLM 总结 + 卡片
├── portfolio/        # ⑨⑨b⑩ Alpaca 持仓 → RiskReport + positions_snapshot + attribution
├── sec/              # ⑪⑫⑫b edgartools wrapper + 8-K item parser + Form 4 + cluster + 5.02 NER + 10-Q parser + insider digest
├── macro/            # ⑭⑮⑯⑰ FRED + yfinance + Tavily + LLM template-fill + FOMC fomc_parser
├── streamer/         # 盘中 runner + universe 扫描器
├── broker/           # Alpaca paper 账户适配器（只读，+get_portfolio_history）
├── universe/         # TECH_30 + 90 ticker → 行业 ETF 映射（含 OTHER 桶）
├── reporting/        # Telegram 格式化器 + notifier + matplotlib + priority + 8 个 4-layer shim
├── memory/           # ChromaDB 驱动的 AnomalyMemory
├── archive/          # 每条推送的 SQLite 日志（离线查询 + RAG）
├── bot/              # Telegram bot：命令、意图分类、响应器、状态
├── scheduler/        # APScheduler 配置 + 子进程 job
└── observability/    # FD / DeepSeek / Tavily / intent classifier monkey-patch trace

scripts/  (21 cron + 3 service entrypoints)
├── run_scheduler.py / run_telegram_bot.py / run_streamer.py
├── daily_screen_to_telegram.py        # ①
├── anomaly_to_telegram.py             # ②
├── lateral_to_telegram.py             # ③
├── institutional_to_telegram.py       # ④
├── backfill_13f.py                    # ④b
├── etf_daily_snapshot.py              # ⑤
├── earnings_reminders.py              # ⑦
├── earnings_summaries.py              # ⑧
├── portfolio_risk_to_telegram.py      # ⑨
├── portfolio_snapshot.py              # ⑨b
├── portfolio_weekly_to_telegram.py    # ⑩
├── sec_8k_to_telegram.py              # ⑪
├── sec_form4_to_telegram.py           # ⑫
├── sec_insider_digest_to_telegram.py  # ⑫b
├── ark_alerts_to_telegram.py          # ⑬
├── macro_daily_snapshot.py            # ⑭
├── macro_release_to_telegram.py       # ⑮
├── macro_claims_to_telegram.py        # ⑯
├── macro_weekly_to_telegram.py        # ⑰
└── p2_digest_to_telegram.py           # 📋
```

---

## 快速开始 · 部署

### 前置依赖

- Python 3.11 + Poetry
- API keys：DeepSeek、financialdatasets.ai、Tavily、OpenAI、Telegram bot token、Alpaca（paper）

### 本地安装

```bash
git clone <repo-url> hedge-fund
cd hedge-fund
poetry install --no-root

cp .env.example .env
# 填入：
#   DEEPSEEK_API_KEY, FINANCIAL_DATASETS_API_KEY, TAVILY_API_KEY,
#   OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#   APCA_API_KEY_ID, APCA_API_SECRET_KEY   （Alpaca — paper 账户免费）

# 冒烟测试单个 agent
poetry run python scripts/daily_screen_to_telegram.py

# 前台跑 scheduler（可选 --test 把每个 job 跑一次再退出）
poetry run python scripts/run_scheduler.py

# Bot / streamer（--test-now 绕过市场时段强制单次扫描）
poetry run python scripts/run_telegram_bot.py
poetry run python scripts/run_streamer.py
```

### 生产部署（Ubuntu 24.04 + systemd）

三个 service unit（替换 paths / user 后 `systemctl daemon-reload && systemctl enable --now hedge-fund-{scheduler,bot,streamer}`）：

```ini
# /etc/systemd/system/hedge-fund-scheduler.service
[Unit]
Description=AI Hedge Fund Scheduler
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hedge-fund
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/root/hedge-fund"
ExecStart=/root/.local/bin/poetry run python scripts/run_scheduler.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/root/hedge-fund/logs/scheduler.log
StandardError=append:/root/hedge-fund/logs/scheduler.err

[Install]
WantedBy=multi-user.target
```

`hedge-fund-bot.service` 和 `hedge-fund-streamer.service` 结构相同（替换 `run_scheduler.py` → `run_telegram_bot.py` / `run_streamer.py`，日志路径替换）。

**资源占用**：scheduler ~205 MB、bot ~50 MB、streamer ~80 MB。**1 GB 的 DigitalOcean droplet 跑得很轻松**。

---

## 测试

**474 个 sandbox 单元测试，按 family 分类**：

| Family | 数量 | 覆盖 |
|---|---|---|
| Earnings (⑦⑧) | **57** | byte-equal 21 / priority ladder 17 / pipeline 9 / cron integration 10 |
| Portfolio (⑨⑨b⑩) | **79** | smoke 20 / priority ladder 19 + floor 5 / pipeline edges 10 / byte-equal 12 / cron integration 17 / ⑨b cron 6 |
| SEC (⑪⑫⑫b + 10-Q) | **131** | smoke 21+20 / priority ladder 21 / byte-equal 15+5 / HTML safety 8 / cron integration 20+10 / bot responder 11 |
| Macro (⑭⑮⑯⑰) | **112** | smoke 40 / priority ladder 16 / byte-equal 16 / HTML safety 9 / bot responder 9 / cron integration 22 |
| ARK (⑬) | **35** | smoke 16 / byte-equal + HTML + shim 11 / cron integration 8 |
| 跨切面 | **60+** | archive migration 9 / intent classify 33 / base priority 18 / observability 13 |

**架构守护测试**（cross-cutting，每个 family 都有）：regression test 钉死 `v2/bot/responders.py` + `scripts/*_to_telegram.py` 不能再出现 inline `_format_*` 私有 helper。所有 cron 必须走 `from v2.reporting import` 公共 API。这阻止了 lift-and-shift 工作被未来 "convenient inline" 回滚撤销。

**Byte-equal pin**（24+ cases across 8 formatter families）：所有公共 formatter (`format_earnings_*` / `format_portfolio_*` / `format_sec_*` / `format_macro_*` / `format_ark_*`) 的输出在多 fixture × 多 case 下被 byte-equal 锁死。Cron 推送 == bot 响应 == formatter 输出，三方相同。

**Sandbox 全跑通**：所有 474 个测试在 sandbox 环境直接 `pytest` 全部 pass，无需 v2.data deps（production-only deps 通过 sys.modules stub harness 替代）。

---

## 工程亮点

### Push vs Pull 语义分离

`/13f BRK` 永远返回最新已知的组合，无论 scheduler 之前是否推过同一份 filing。**cron 路径会和 `edgar.db` 去重以避免重复推送**；**bot 路径完全跳过这个检查**。一个底层 pipeline 两套调用接口，对应两种访问模式。Streamer 上同理——用户 `/alert` 和 TECH_30 自动扫描是两条独立路径共享 streamer 主循环，一个失败不会压制另一个。

### 13F 解析的 CUSIP 聚合

Berkshire 通过三个子公司持 AAPL（BHRG、GEICO、National Indemnity），13F-HR 信息表把它们记成三行 **相同 CUSIP** 的独立条目。naive 的 `INSERT OR REPLACE` 会无声丢掉其中两条。修复：EDGAR parser 层做聚合——一个 CUSIP 一行，shares 和 market value 求和。**Berkshire 的 AAPL 持仓从错误的 $958M 修正为正确的 $57.8B（组合占比 22%）**。

### 严格枚举意图分类

NL 分类器输出 23 个意图之一加上参数。输出 JSON 被解析、被白名单 set 校验。**LLM 永远不决定要"说"什么，只决定要"调用"哪个工具**。如果用户想让 DeepSeek 凭想象写金融分析，他们可以直接问 DeepSeek。这个系统存在的意义是 grounded、多源、可验证。

### 原子触发的 Alert + 板块基准护城河

Streamer 的 `alert_fire_check` 跑的是 `UPDATE alerts SET fired_at=? WHERE id=? AND fired_at IS NULL`。`fired_at IS NULL` 守卫让操作在并发 streamer 实例下幂等——**one-shot 语义在 SQL 层而不是应用层强制**。每个 agent run 启动时预取 3 个 ETF 序列（SPY/XLK/SMH），让每条异动都自带相对强度对比，不烧 LLM token。

### 信源 tier 防御 + 过期数据 chip

Verifier prompt 显式列出 Tier 1/2/3 domain 清单，Generator 只能看到 Tier 1 + Tier 2 来源。**静默丢弃比自信乱讲更重要**。Greenlight Capital 最新 13F 是 2023-Q4——不打 ⚠️ chip 等于无声把 29 个月前的仓位当作 Einhorn 的当前状态。chip 把时间边界显式呈现给用户。

### 盘中 Streamer 刻意不调 LLM

交易时段里 streamer 不调 Tavily、不调 DeepSeek、甚至连新闻层都不拉。理由：盘中新闻滞后于价格。如果 10:15 触发归因，模型其实没有真实素材可用，**只会瞎编**。盘中卡片显式指向 `/why TICKER` 并标明深度归因在 17:35 ET 才可用。

### 8 个 4-layer formatter shim 保证 byte-equal

`format_*` 公共 API source-of-truth 在 `v2/{earnings,portfolio,sec,macro,etf}/_bot_cards.py`（sandbox-runnable，无 v2.data deps），通过 `v2/reporting/_*_formatters.py` 4 层 re-export shim 暴露公共名，cron + bot 共用同函数对象（identity check 测试 pin）。这是跨 surface byte-equal 的工程基石。

### 异步安全的长任务

`/why NVDA` 类 20 秒级 pipeline（FD + Tavily + DeepSeek × 2）通过 `asyncio.get_running_loop().run_in_executor(None, sync_fn, *args)` 投递。Polling 循环保持响应——单用户场景里其实无所谓，但模式是对的。

### Stage 0 audit + Phase 3.5/5a silent-ship 防御

Phase 5a Stage 0 audit 发现 v2/etf/ 已经覆盖 ARK CSV 抓取 → scope pivot **节省 87% 代码 / 31% 工时** (1500→250 行)。Phase 3.5 Stage 4 cron integration test **抓出 Stage 2 silent-ship bug**——priority.py 加了 `+20 going_concern_in_10q` 规则但 cron 没 forward 进 metadata，end-to-end 测试是唯一防御。Phase 5a Stage 4 明确 mirror 这个 pattern：assert `priority_reasons` trail 含 literal `multi_fund_coordination` / `held_or_watchlist_ark` 字符串。

---

## 成本分析

真实部署的月度运营成本：

| 项目 | 成本 |
|---|---|
| DigitalOcean droplet (1 GB) | $6.00 |
| financialdatasets.ai（cached） | ~$15 |
| DeepSeek tokens（~3M/月，所有 agent + bot 合计）| ~$1 |
| OpenAI embeddings（~50K dims/月）| ~$0.10 |
| Tavily News（~500 queries/月）| $0（免费层）|
| Alpaca（paper 账户，IEX 行情）| $0 |
| **合计** | **~$22/月** |

`CachedFDClient` 层通过 per-endpoint TTL SQLite 缓存（基本面 24h、价格 6h、公司信息 7d；新闻刻意不缓存）**消除了约 65% 的 FD 调用**。盘中 streamer 在交易时段每分钟新增 ~30 个 Alpaca 调用，远低于免费层 200/min 限制。

---

## Roadmap

### ✅ Shipped (Phase 0–5a)

| Phase | 简述 |
|---|---|
| **Phase 0** | Push priority 系统（P0/P1/P2/P3 + importance_score + P2 digest cron） |
| **Phase 1** | Earnings Agent（⑦⑧ + `/earnings`） |
| **Phase 2** | Portfolio Risk Agent（⑨⑩ + `/risk` + `/pnl`） |
| **Phase 2.5-mini** | BROAD-market ETF bucket（IVV/SPY/VOO/QQQ classification） |
| **Phase 2.5 完整版** | per-position attribution（⑨b 16:25 ET sub-cron + `positions_snapshot` 表 + ⑩ Friday Brinson-style attribution）+ drawdown realtime fix（`today_realtime_value` kwarg 解决 2026-06-05 prod-bug） |
| **Phase 3** | SEC 监控 Agent（⑪⑫ + `/8k` + `/insiders`）+ 24-item 8-K priority 表 + Form 4 noise/signal 区分 + 同日 ≥3 cluster 检测 + 5.02 LLM NER + HTML 安全 lint |
| **Phase 3.5** | 10-Q parser + ⑧ 10-Q MD&A diff 集成（going_concern P0 / material_weakness +15）+ ⑫b SEC Insider Weekly Digest（Fri 19:15 ET, title-only 简化口径） |
| **Phase 4** | Macro Agent（⑭⑮⑯⑰ + `/macro` + `/cpi` + `/fomc` + `/yields`）+ FRED+yfinance hybrid 数据源 + LLM 4 层防御 + 硬编码 release_calendar + bps 行业标准格式 |
| **Phase 4.5-mini** | FD → yfinance daily prices migration（`PriceSource` Protocol + `V2_PRICE_SOURCE=fd` 一键 fallback）。消除 `today - 3` buffer：①/②/③/`/why`/`/summary` 卡片日期现在显示 today |
| **Phase 5a** | ⑬ ARK 调仓告警（Mon-Fri 08:30 ET pre-market）+ multi-fund 协同检测 + 复用 v2/etf/ 基础设施（zero schema change） |
| **Cross-cutting** | Web 仪表盘（FastAPI + React + Tailwind + trace 回放 + QA mode）；观测层 SDK（`v2/observability/` monkey-patch trace） |

### ⏳ Deferred — 待真实数据齐全 / 触发条件满足

> **命名澄清**：Phase 4.5-mini ✅ 已 ship（FD → yfinance daily prices）；Phase 4.5 ⏳ deferred（FOMC transcript + Historical analog）。

#### ⏳ Phase 3.5.5 — Form 4 结构化表

**目标**：`form4_transactions` 结构化表（替代 ⑫b 当前的 title-only 简化口径）：per-A/M/F/G/C 完整 breakdown + 聚合 USD 总额 + insider-name 维度的趋势聚合。

**Trigger**：⑫b 实战周报跑过 1+ 月后，operator 对 per-code breakdown 信息有真实需求反馈。

#### ⏳ Phase 4.5 — FOMC Transcript + Historical Analog (LLM Layer 4)

**目标**：
- **Task A**：FOMC ⑮b transcript +6h follow-up 卡 —— Powell 记者会结束后 6 小时从 Bloomberg / Reuters Q&A 摘要抓取，补 statement diff 之外的语调信号
- **Task B**：Historical analog mode —— LLM Layer 4 防御正式落地，LLM 引用"过去 5 次类似 σ surprise 的 SPX 5-day return"作为预测尺度

**Trigger**：06-17 FOMC + SEP 实战完成 + Phase 4 ⑮⑰ 跑过 ≥ 2 个完整月份 + 用户对 Layer 1+2 defense 有真实反馈。**工时**：1.5-2 天 · **风险**：中。

#### ⏳ Phase 5b — Market Regime Detection

**目标**：综合 VIX + 10Y-2Y spread + breadth + sentiment 判断市场 regime（risk_on / risk_off / transition / extreme_vol），新 ⑭b cron Mon-Fri 16:35 ET，regime 切换 → P0 告警，per-regime 持仓表现归因（跟 ⑨b snapshot 集成）。

**Trigger**：Phase 4 全套实战 ≥ 4 周 + Phase 4.5 已 ship 验证 Layer 3/4 防御稳定 + 用户对 macro 信号有判断尺度。**工时**：2-3 天 · **风险**：高（regime detection 容易 backtest 漂亮但 live 不灵）。

#### ⏳ Phase 6 — News 重构 + 3-tier Universe

**目标**：News 层重构（集中 Tavily query + cache + NER ticker mapping）+ 3-tier Universe（Tier 1 核心 / Tier 2 watchlist / Tier 3 广义观察，各 tier 不同 cron 频率）。

**Trigger（任一）**：universe ≥ 30 ticker；OR LLM cost 月度超 $50；OR ≥ 5% 推送是 Tier 3-4 noise。**工时**：4 天 · **风险**：中。

### 持续 / 待办

- Migration system audit
- GitHub Actions CI（pytest 在 main 上跑）
- 多用户支持（每个 chat 独立 watchlist + 持仓）
- 接入现有 v2 event-study 框架做 backtest
- Alpaca 从 paper 切换到 live（paper 充分验证后一个配置开关）

> 当前处于 production observation 阶段，focus 是 validate Phase 0–5a 在真实数据下的稳定性。

---

## 致谢 · License

仓库外壳和原始教育版 `app/` 目录来自 [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)——一个开源 AI hedge fund 概念项目。`v2/` 目录——也就是本 README 描述的整个另类数据 agent 系统（21 个 cron job、盘中 Streamer、含 23 个 NL intent 的 Telegram bot、5 层防幻觉机制、推送优先级系统、所有 SQLite + ChromaDB 存储、所有 systemd 部署脚手架）——是在那个基础上**完全从零搭建的独立项目**。

License: **MIT**，教育用途，**不构成投资建议**。
