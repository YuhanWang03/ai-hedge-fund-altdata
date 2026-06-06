# AI Hedge Fund · 另类数据 Agent 系统

一个生产级的另类数据情报平台，以 Telegram bot 作为交付界面。**六个盘后批处理 agent** 持续监控美股市场，**两个财报 cron** 自动追踪 watchlist + 持仓的 D-3/D-1/D-0 提醒和发布后总结，**两个组合风险 cron** 推送每日集中度 / P&L / 回撤快照 + 周度复盘，**两个 SEC 监控 cron** 每日扫描 8-K 重大事件 + Form 4 内部人交易（含同日 ≥3 distinct insiders 集群检测），**一个 SEC 周度内部人摘要 cron** 每周五 19:15 ET 聚合本周 ⑫ 推送 → 单卡周报，**四个 Macro Agent cron** 推送每日 VIX / yields snapshot + CPI/PCE/NFP/GDP/PPI 单日 release + FOMC statement diff 与 SEP dot plot + 周度 macro recap，**一个盘中 Streamer 服务**实时扫描异动并触发用户价格提醒，**二十三个自然语言意图**处理交互式查询，**五重防幻觉机制**保证每一条推送都有可追溯的源头。

定位为简历展示型项目，旨在展现端到端拥有"多源数据管道 + 双 LLM 校验架构 + 7×24 部署系统"的能力。

[English README](./README.md)

---

## 它能做什么

系统在一台 $6/月 的 VPS 上并发运行三个服务：

**Scheduler（调度器）** —— 二十个 cron 任务，覆盖基本面筛选、异动检测、产业链横向扩展、机构 13F 跟踪、周度回填、ETF 每日持仓抓取、财报日历提醒、财报发布后总结（含 10-Q MD&A 关键变化 diff）、组合风险快照、组合持仓快照（⑨b 静默后台）、周度复盘、SEC 8-K 重大事件、SEC Form 4 内部人交易、SEC 周度内部人摘要（⑫b Fri 19:15 ET）、Macro 日终 snapshot、Macro release scanner、Macro Initial Claims、Macro 周报、P2 日汇总、archive 清扫。每个任务进程隔离，单个崩溃不会污染其他。

**Streamer（实时扫描）** —— 分钟级盘中扫描器。在美股交易时段（9:30–16:00 ET）轮询 Alpaca 实时行情，做两件事：检查用户设置的价格提醒是否触发；扫描 TECH_30 universe 寻找双门槛异动（≥3% 价格变动 **且** ≥2.5× 成交量节奏）。**盘中绝对不调用 LLM / Tavily** —— 深度归因留给 17:35 ET 的盘后 agent。

**Telegram Bot** —— 24/7 长轮询机器人，支持 25 个 slash 命令、3 个 watchlist 命令，以及一个含 23 种意图的自然语言分类器。通过 chat ID 过滤实现单用户授权。

三个服务通过 WAL 模式共享 7 个 SQLite 数据库 + 一个 ChromaDB 向量库（用于 RAG 记忆）。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-scheduler.service                 │
├──────────────────────────────────────────────────────────────────┤
│  08:00 ET 周一-周五  ⑦ 财报日历提醒        → Telegram 推送       │
│  09:00 ET 周一-周五  ⑮ Macro Release        → Telegram 推送       │
│  09:30 ET 周四       ⑯ Macro Initial Claims → Telegram 推送       │
│  16:25 ET 周一-周五  ⑨b 持仓快照（静默）   （后台 archive only）  │
│  16:30 ET 周一-周五  ⑭ Macro 日终 Snapshot  → Telegram 推送       │
│  17:00 ET 周一-周五  ⑤ ETF 每日持仓        （静默写入）          │
│  17:05 ET 周一-周五  ⑪ SEC 8-K Scanner      → Telegram 推送       │
│  17:30 ET 周一-周五  ① 基本面筛选          → Telegram 推送       │
│  17:35 ET 周一-周五  ② 异动监测            → Telegram 推送       │
│  17:45 ET 周一-周五  ⑫ SEC Form 4 Scanner   → Telegram 推送       │
│  18:00 ET 周一       ③ 产业链横向扩展      → Telegram 推送       │
│  18:00 ET 周二/周五  ④ 机构 13F             → Telegram 推送       │
│  18:30 ET 周一-周五  ⑨ 组合风险快照        → Telegram 推送       │
│  18:30 ET 周日       ④b 13F 周度回填        （静默刷新）         │
│  19:00 ET 周五       ⑩ 组合周度复盘        → Telegram 推送       │
│  19:15 ET 周五       ⑫b SEC 内部人周报      → Telegram 推送       │
│  19:30 ET 周五       ⑰ Macro 周报           → Telegram 推送       │
│  21:00 ET 周一-周五  ⑧ 财报发布后总结      → Telegram 推送       │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-streamer.service                  │
├──────────────────────────────────────────────────────────────────┤
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
├──────────────────────────────────────────────────────────────────┤
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

## 六个盘后 Agent

### ① 基本面筛选（周一-周五 17:30 ET）

针对 TECH_30 universe 做硬规则筛选（市值、营收增速、毛利率、波动率），通过的候选打上 9 个定性标签（高成长 / 高毛利 / 小盘 / 业绩超预期 等）。最终给每只通过的标的生成"模板填充式"LLM 叙述——**模型只输出定性逻辑，所有数字由 Python 注入**，从源头消除数字幻觉。

每个候选同时附加**板块相对强度对比**（半导体 → SMH，其他科技 → XLK），把单股指标看不到的"领涨 / 掉队 / 同步"格局展现出来。

通过的候选还会拉取近 7 日 Tavily 新闻标题 + 1 周回报相对同业中位数的差值。

### ② 异动监测（周一-周五 17:35 ET）

在 TECH_30 universe 上检测三类异动信号：
- 成交量 spike（≥3× 30 日均值）
- 52 周新高 / 新低
- 内部人大额买卖簇（基于 Form 4 公开市场交易）

每个触发的异动：

1. 自动加上**板块相对强度 chip**（当 ticker 与板块反向移动且差距 ≥1.5pp 时打 `★ 逆势`）
2. 进入多源归因管线：
   - Tavily 新闻搜索 → 实体过滤（ticker + 公司名 + 高管名匹配）
   - Verifier LLM 给每个新闻源打 tier（SEC/Reuters/Bloomberg = Tier 1；MarketWatch/SeekingAlpha/Yahoo = Tier 2；其他 = Tier 3）
   - Generator LLM 只用 Tier 1 + Tier 2 来源做归因合成
3. 结果用确定性 ID `{ticker}_{date}` 持久化到 ChromaDB，供 RAG 检索

### ③ 产业链横向扩展（周一 18:00 ET）

把前一日异动的 ticker 当作种子，让 DeepSeek 提议上下游邻居（供应商 / 客户 / 同业小盘 / 受益方）。每条提议的关系再经过一次 Tavily 搜索验证——**要求种子和邻居 ticker 在同一新闻中共现**，否则该关系被丢弃。

通过验证的邻居再做市值 / 营收增速 / 毛利率门槛筛选才会被推送。

### ④ 机构 13F（周二/周五 18:00 ET）

从 SEC EDGAR 拉取 10 位明星 manager 的最新 13F-HR：Berkshire（巴菲特）、Scion（Burry）、Pershing Square（Ackman）、Greenlight（Einhorn）、Renaissance、Two Sigma、D.E. Shaw、Citadel、Coatue、ARK（Cathie Wood）。

对每个新 filing 计算季度对比的仓位变动，分类为"新进 / 加仓 / 减仓 / 清仓"，前 20 笔变动喂给 DeepSeek 做十字以内的策略性解读。

**当前追踪 $1.29 万亿 AUM × 17,329 个仓位**。

### ④b 13F 周度回填（周日 18:30 ET）

每周做一次安全网——重新抓取所有 10 个 manager 的最新两个 filing，幂等地用 CUSIP-aggregated 数据覆盖 DB。捕获修正 filing、EDGAR 偶发错误、以及 Tue/Fri 推送 agent 可能漏掉的竞态。

### ⑤ ETF 每日持仓（周一-周五 17:00 ET）

静默地从 ARK Invest 公开 CDN 抓取 4 只基金（ARKK / ARKW / ARKG / ARKF）的当日完整持仓 CSV，构建 (基金, 日期, ticker) 时间序列，实现 **24 小时调仓检测**——粒度比 45 天延迟的 13F 高了两个数量级。

---

## 盘中 Streamer 服务

与 scheduler 并行运行。交易时段（周一-周五 9:30–16:00 ET）每 60 秒轮询一次，非交易时段 5 分钟一查。

每次轮询独立做两件事：

### A. 用户设置的价格提醒

用户通过 `/alert NVDA 130 above` 或自然语言"提醒我 NVDA 突破 130"创建提醒。Streamer 每分钟：

1. 查 `alerts` 表里所有未触发的条目
2. 把涉及的 ticker 批量打包成一次 Alpaca `get_stock_latest_trade` 请求
3. 对每个 ticker 跑 `alert_fire_check(ticker, current_price)` —— SQL 原子性地标记任何已突破的提醒（`UPDATE … WHERE fired_at IS NULL`）并返回
4. 每个触发的提醒推送一张卡片；永远不重复触发（设计上的 one-shot 语义）

### B. TECH_30 自动异动扫描

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

## Telegram 交互界面

机器人的自然语言层把任意中文 / 英文文本归类到 **23 个固定意图**之一，分类器是 DeepSeek temperature=0 + JSON 输出 + 白名单枚举校验。**任何不在白名单的输出都 fallback 到 `unknown`**，保证行为有界。

### Slash 命令

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
| | `/pnl [day\|week\|month]` | 盈亏（默认 day = Phase 0 行为；week/month 走 ⑨⑩ 数据）|
| | `/risk` | 实时组合风险快照（同 ⑨ cron，read-only）|
| SEC | `/8k TICKER` | 过去 30 天 8-K 摘要 + 5.02 LLM 抽取（同 ⑪ cron 数据源） |
| | `/insiders TICKER [days]` | 过去 N 天 Form 4 摘要（默认 90，bounded 7-365）|
| Macro | `/macro` | 综合宏观 dashboard（VIX / yields / 最近 + 下次 release）|
| | `/cpi` | 最新 CPI release 卡（含 surprise σ + summarizer 输出）|
| | `/fomc` | 最近 FOMC 决议（statement diff + SEP + Tavily 卖方读数）+ 下次会议预告 |
| | `/yields` | 收益率曲线（复用 `/macro` dashboard）|
| 元信息 | `/settings`, `/help`, `/start` | 阈值查看 + 命令参考 |

### 自然语言示例

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

### Manager 别名（支持 10 位）

`brk / berkshire / buffett`、`burry / scion`、`ackman / pershing`、`einhorn / greenlight`、`renaissance / rentech`、`twosigma`、`deshaw / shaw`、`citadel`、`coatue`、`ark / cathie / wood`

---

## 五重防幻觉机制

系统中的 LLM **从不直接产出数字事实**。它们只做分类决策、写定性叙述、在候选信源里做选择。**所有数字都来自原始数据源**。

| 层 | 机制 |
|---|---|
| 1. 实体过滤 | 新闻必须同时包含目标 ticker AND 至少一个公司名 token，才送给 Verifier |
| 2. 信源 tier 评分 | Verifier LLM 给每个信源打 Tier 1（SEC, Reuters, Bloomberg, WSJ, FT, CNBC）/ Tier 2（MarketWatch, SeekingAlpha, Yahoo, Fool, Forbes）/ Tier 3（其他）。**Tier 3 直接丢弃** |
| 3. Generator-Verifier 分离 | 独立的 Verifier LLM 验证 Generator 输出是否能用信源支撑，prompt 严禁出现输入未提及的数字 |
| 4. 模板填充模式 | 筛选叙述器只输出定性短语；Python f-string 注入所有数字。LLM 没机会读错指标 |
| 5. 过期数据 chip | filing > 180 天显示 ⚠️；> 365 天显示 ⚠️ 已 N 月未更新。防止用户把 2023 年的 13F 当作当前状态 |

---

## 推送优先级系统 P0/P1/P2/P3

每个推送在 emit 时由 `v2/reporting/priority.py` 计算 `importance_score`
（0–100），映射为 4 个 tier：

| Tier | Score | 行为 | 示例 |
|---|---|---|---|
| **P0** | ≥ 80 | 立即推 + 🚨🚨🚨 前缀 + Dashboard 红框 | 价格 alert 触发、组合大额亏损、FOMC 决议、ARK 清仓持仓股、重大 8-K |
| **P1** | 60–79 | 立即推（无前缀） | 异动归因、watchlist 财报、13F 变动 |
| **P2** | 40–59 | 入 archive，16:45 ET 汇总一条 | 日筛选、产业链候选、ETF 静态快照 |
| **P3** | < 40 | 仅 archive，Dashboard 默认隐藏 | 弱归因异动、scheduler 心跳 |

### 打分规则

每类事件有一个**基础分**（见 `BASE_SCORES`），然后由 metadata 调整：

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

最终 score 在 0–100 之间 clamp，并按 80/60/40 阈值映射到 P0–P3。

### 工作流

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

### 工程注释

- **向后兼容**：notifier.send_text 若不传 priority，默认走 P1 路径，所有
  老代码继续工作
- **不参与 LLM**：纯 Python 规则，本地即时算出
- **archive 表结构**：新增三列 `importance_score / priority_tier /
  priority_reasons` 通过 PRAGMA table_info 幂等迁移
- **老记录处理**：priority_tier IS NULL 的旧推送在 Dashboard 端按 P1 显示

---

## ⑦⑧ Earnings Agent · 财报追踪

第七和第八个 agent 是一对配合的财报 cron：⑦ 在每个交易日开盘前把
watchlist + Alpaca 持仓里**即将发财报**的 ticker 提前推到 Telegram，
⑧ 在收盘后把**当日发完财报**的 ticker 拉 FD 实际数据、按 LLM Template-Fill
模式生成总结卡。两者都接入 ⑥ 推送优先级系统，持仓股自动 +15、watchlist +10，
重大 surprise 升 P0。

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑦ Earnings Reminders | Mon-Fri **08:00 ET** | 检查 watchlist + 持仓 的 D-3/D-1/D-0 提醒 | D-3 = P2，D-1/D-0 = P1 |
| ⑧ Earnings Summaries | Mon-Fri **21:00 ET** | 当日发布的财报数据落地后 LLM 总结，否则标 pending 次日重试 | 基础 P1（70），\|surprise\| ≥ 10% 升 P0（+30） |

### 数据源

| 用途 | 源 | 备注 |
|---|---|---|
| 未来财报日历 | yfinance `Ticker.calendar` | FD 没有 forward calendar，必须 yfinance |
| 上次实际 vs 预期 | FD `get_earnings` | source-of-truth，按订阅计费 |
| 历史 surprise 趋势 | FD `get_earnings_history` | 最近 4 季用于 streak 展示 |
| Transcript URL | Tavily 搜索 | best-effort，可空 |

### 输出示例

**提醒卡（D-1，watchlist 股，P1）：**

```
⏰ 财报提醒 · AAPL · D-1
发布日：2026-06-07（盘后）
👁 关注列表

EPS 预期：1.51
营收预期：$94.00B
```

**总结卡（BEAT 持仓股 + 15% surprise，P0）：**

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

### Phase 3.5 · 10-Q MD&A 集成（⑧ 增强）

⑧ 每次推送总结卡时同步反查该 ticker 过去 200 天的最新 10-Q，与上一季 10-Q
对比 MD&A（Part I Item 2）+ Risk Factors（Part II Item 1A）差异。卡片末尾
追加 "📋 10-Q MD&A 关键变化" 区块：**新增段落最多 3 条，每条 cap 80 字 +
"…" 截断后缀**；新 risk factor heading 数量；conservative auditor flags
（"going concern" / "material weakness" 正则命中）。

10-Q 抓取失败（EDGAR 504 / 解析异常 / 该季无 10-Q）→ 区块**静默省略**，
earnings 卡照常推送。conservative auditor flags 直通 priority 系统：
`has_going_concern` 触发 **+20** priority bump（70 base → 90，足以升 P0），
`has_material_weakness` 触发 **+15**。两个 flag 都直接 surface 到卡片上
不留死角。

### 工程亮点

- **yfinance / FD 失败 silent skip** —— 单个 ticker 抓不到日历不阻塞整批
- **D-N 不缓存日期** —— 每天 cron 重算，自动吸收公司临时改 release 日的情况
- **Pending 重试机制** —— FD 数据当日没落地时推一条 P2 "数据待落地" 占位，
  archive 标 `pending_<today>` 不和真实 `report_period` 冲突；次日 21:00 ET
  cron 自动重试
- **持仓股自动升 priority** —— `earnings_reminder_d3` 基础 45（P2），加 15 持仓
  bonus 后变 60（P1）；`earnings_summary` 70 + 大 surprise +30 + 持仓 +15 → 100
  → P0
- **Template-Fill 模式** —— LLM 只写定性的 bull/bear/narrative，所有数字
  （EPS、营收、surprise %）由 Python 注入卡片模板
- **跨季度去重** —— `archive.earnings_summarized` 表 `PRIMARY KEY (ticker,
  report_period)`，同季度只推一次；只有 `outcome = 'summarized'` 的行进
  `get_summarized_set()`，pending 行不会误判已完成

### 成本

- **yfinance**：免费
- **FD `get_earnings` / `get_earnings_history`**：包含在 $15/月订阅（每次调用走
  `cached_fd` 24 小时 TTL，同季多次查询只算一次）
- **Tavily 搜 transcript URL**：每条 summary ~$0.005，可关
- **DeepSeek（template fill）**：每条 summary ~$0.001
- **每周 ~5 个 watchlist ticker × 一次发布 ≈ $0.03/月** —— 实际月成本 < $0.10

### 用户主动查询

也可以随时通过 bot 主动查：

```
/earnings AAPL        # 单股财报卡（next + last + ⭐ 标记）
/earnings             # 未来 14 天日历（watchlist ∪ 持仓）
苹果什么时候发财报     # → earnings_view AAPL
下周谁要发财报         # → earnings_calendar days_horizon=7
```

这条路径是 read-only —— 不走 cron，不写 archive，不打 priority。

---

## ⑨⑨b⑩ Portfolio Risk Agent · 组合风险监控

第九和第十个 agent 是一对配合的组合风险 cron：⑨ 每个交易日盘后推送当日
风险快照（集中度 + 行业暴露 + P&L + 回撤 + 7 天财报风险），⑩ 周五额外
推送周度复盘 + per-position 周表现归因。Phase 2.5 完整版又加了 ⑨b 静默
后台任务，每个交易日 16:25 ET 把当日持仓快照写入 `positions_snapshot`
表（5 天 rolling window 喂给 ⑩ 的 attribution 计算）。三者都接入 ⑥ 推送
优先级系统：正常一天 P2 静默；单日亏损 ≥ 5% 或多因子叠加自动升 P0；
周报始终 P1（操作员可见）。

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑨ Portfolio Risk | Mon-Fri **18:30 ET** | 实时拉 RiskReport 全景（drawdown 含 today realtime fix）| 见 ladder |
| ⑨b Positions Snapshot | Mon-Fri **16:25 ET** | 静默后台：写当日持仓快照到 `positions_snapshot` 表 | 无推送（archive trace 仅） |
| ⑩ Portfolio Weekly | Fri **19:00 ET** | 周复盘 + 月回报 + 1M 回撤 + 行业暴露 + **per-position attribution** | 固定 P1（floor）|

### 数据源

| 用途 | 源 | 备注 |
|---|---|---|
| 当前持仓 + 现金 | Alpaca `account` + `positions` | TOTAL = invested + cash，invested_value 是派生 |
| 1M equity 历史 | Alpaca `portfolio_history(1M, 1D)` | 算 1W / 1M 回报 + 峰-谷回撤 |
| 行业 ETF 映射 | `v2/universe/etfs.py` 90 个 ticker | OTHER 桶覆盖未映射 |
| 7 天财报风险 | 复用 `v2.earnings.calendar` | 不重复实现 yfinance 调用 |

### Priority Ladder（⑨ 专用）

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

### 输出示例

**⑨ 多因子 → P0 卡：**

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

**⑩ Clean Week P1 卡（floor 触发，Phase 2.5 完整版含 per-position attribution）：**

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

**Attribution 3-state gating** (Phase 2.5 完整版):
- ≥5 天 ⑨b snapshot 覆盖 → 最佳 / 最差 / 净贡献 三行（worst 行在
  单持仓周自动跳过避免重复）
- 1-4 天 → 显示 `<i>归因数据累积中 (N/5 天)</i>` 占位
- 0 天 → 完全静默（fresh-account contract，不为缺数据道歉）

**Drawdown realtime fix** (Phase 2.5 完整版): Phase 2 时 `compute_drawdown`
只看 `get_portfolio_history()` 的 EOD canonical 序列（截止昨日收盘），
今天的 intraday 下跌不进 series → 卡片矛盾显示 "今日 P/L -3.72% / drawdown
0.00%" (2026-06-05 prod 实测)。Fix: `compute_drawdown(broker,
today_realtime_value=portfolio_value)` 把 Alpaca real-time portfolio_value
append 到 EOD 序列（或 overwrite 今天点 idempotent on rerun），peak /
max_dd / current_dd 自动包含今天 → 卡片即时反映真实下跌。Backward-compat
保留：`today_realtime_value=None` 行为同 Phase 2。

### 工程亮点

- **portfolio_value 是 TOTAL**（Alpaca 原值 = invested + cash），`invested_value` 派生 @property 防漂移
- **weight 分母是 invested_total**（持仓市值之和），cash 不参与集中度（"Top 1 NVDA 35%" 是直觉的"持仓里 35%"）
- **drawdown 5 层符号防御**：compute 返回非负 → 内部 `assert ≥ 0` → renderer 强制 `-` 前缀 → 阈值用 raw 不加 abs → 调用方传 signed/unsigned 都行（`compute_importance` 用 abs 兜底）
- **Alpaca 不可用 silent recovery**：任何子模块 failure 写 `RiskReport.warnings`，cron 仍推一条 P2 "数据不全" 卡而不是静默不发
- **byte-equal 跨 surface**：⑨ cron 推送 == `/risk` bot 响应 == `format_portfolio_risk_card(report)` 三方相同
- **行业 ETF 映射扩展到 90 个 ticker**（Phase 0 时仅 29 个 tech），未映射进 OTHER 桶不进 SPY 兜底——避免"你持有的未知股"被假装成"大盘暴露"

### Bot 接口

| 命令 | 行为 |
|---|---|
| `/risk` | 实时拉 RiskReport（read-only，不写 archive 不算 priority） |
| `/pnl` | 当日盈亏（保持 Phase 0 行为，向后兼容） |
| `/pnl week` | 本周 P&L 摘要 |
| `/pnl month` | 本月 P&L 摘要 |
| NL "组合风险怎么样" | → `risk_view` |
| NL "这周亏了多少" | → `pnl_period(period="week")` |
| NL "本月赚了多少" | → `pnl_period(period="month")` |

### Phase 2.5 完整版 (shipped, commit `54fb117` 系列)

Phase 2 ⑩ 当时显式标 "per-position 归因待开发 → Phase 2.5"，因为 Alpaca
不直接提供 per-position 历史曲线。Phase 2.5 完整版 自建 `positions_snapshot`
表 + ⑨b sub-cron 每个交易日 16:25 ET 写入快照，⑩ Friday 19:00 ET 读最近
7 天窗口 + `compute_weekly_attribution(snapshots, current_positions)` 算
Brinson-style per-position 周表现归因（`contribution = avg_weight ×
weekly_return`）。同时 fix 了 ⑨ 的 drawdown realtime UX bug（详见上方
工程亮点）。两件事 scope 不重叠但相关 — 都解决组合卡片的"显示不真实/不
完整"问题。

### 测试覆盖

- **20 个 portfolio smoke**（pipeline edges + sign convention + invested_value derivation）
- **5 个 priority floor**（关闭 Stage 2.5 dry-run 发现的 Issue 3）
- **19 个 priority ladder**（覆盖 8 个 spec ladder 案例 + multi-factor 叠加 + held-trumps-watchlist）
- **12 个 byte-equal pin**（4 个 formatter × 多 fixture；Phase 2.5 完整版加 3 个 attribution 渲染 case）
- **17 个 cron integration**（priority threading / archive write / trace_json / responder_name 标签 / CronTrigger day_of_week pin / byte-equal cross-surface；Phase 2.5 完整版加 4 个 attribution + drawdown realtime 端到端 case）
- **20 个 Phase 2.5 完整版 smoke**（snapshot write/read/replace + attribution math + drawdown today_realtime_value append/overwrite/recompute + 公共 surface contract）
- **6 个 ⑨b cron integration**（snapshot 写入 / Alpaca-down 静默 / 同日 rerun REPLACE / responder_name pin / silent-no-Telegram 守护 / 空持仓静默）

总计 79 个 Phase 2 + Phase 2.5 完整版 累计测试。

---

## ⑪⑫ SEC 监控 Agent · 8-K + Form 4 内部人

第十一和第十二个 agent 是一对每日 cron：⑪ 17:05 ET 扫 universe (held + watchlist) 当天 8-K 重大事件（按 24 个 item code 优先级表分级，2.02 跳过给 ⑧ 处理），⑫ 17:45 ET 扫 Form 4 内部人交易（P/S 信号单独推送，A/M/F/G/C 走 noise_summary 不打扰，同日 ≥3 distinct insiders 同方向 → cluster 卡）。同时暴露 `/8k` 和 `/insiders` bot 命令做 on-demand 查询，复用同一公共 formatter 保证 byte-equal。

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑪ SEC 8-K | Mon-Fri **17:05 ET** | 单 ticker 当日 8-K → 单卡聚合所有 items | base by max item tier，5.02 senior_exec +15 |
| ⑫ SEC Form 4 | Mon-Fri **17:45 ET** | P/S 单笔卡 + 同日 ≥3 cluster 卡 | base 75 (P) / 50 (S) + magnitude / role / 10b5-1 调整 |

### 数据源

- **edgartools 5.31.5**（SEC EDGAR API wrapper），200ms throttle 节流
- 复用 Phase 1 ⑦⑧ 的 `EDGAR_IDENTITY` env var（SEC 要求 User-Agent 含联系邮箱）
- 单独 `v2/sec/` 模块，与 `v2/institutional/`（13F 路径）解耦——8-K item parsing 和 Form 4 transaction parsing 与 13F 季度持仓快照零代码复用

### ⑪ 8-K Scanner — 24 个 Item Codes 优先级表

| Tier | Item codes |
|---|---|
| **P0** | `1.03` Bankruptcy · `1.05` Cybersecurity · `2.04` Triggering events on off-BS · `3.01` Notice of delisting · `4.02` Non-reliance on prior financials · `5.01` Change in control · `5.02` Senior exec departure（LLM 确认） |
| **P1** | `1.01` Material agreement · `1.02` Termination · `2.01` Acquisition / disposition · `2.03` Material obligation · `2.05` Restructuring · `2.06` Material impairment · `4.01` Change in auditor · `5.02` Other officer changes |
| **P2** | `3.02` Unregistered sales · `3.03` Material modification of rights · `5.08` Shareholder director nominations · `7.01` Reg FD · `8.01` Other events |
| **P3** | `1.04` Mine safety · `5.03` Bylaw amendment · `5.04` Suspension of blackout period · `5.05` Amendments to ethics code · `5.07` Submission to shareholder vote · `9.01` Financial exhibits |

**`2.02` Results of Operations 严格跳过** —— 该数据由 ⑧ Earnings Summaries 在 21:00 ET 处理（财报正式 actual + LLM 总结）。混合 2.02 + 其他 items 的 filing 保留卡片但 2.02 行注 "(⑧ 处理)"；纯 `{2.02}` 或 `{2.02, 9.01}` 的 filing 整张丢弃。

**多 item 单卡**：HPE 风格的一张 8-K 同时声明 1.01 + 2.02 + 5.02 + 7.01 + 9.01 时合并成一张卡，`max_priority_tier` 决定整条 priority。Stage 0 真实数据校准结果：分裂成 5 张卡会污染推送频道。

**5.02 LLM 抽取**：`v2/sec/ner_5_02.py` 用 DeepSeek template-fill 提取 `{departures, appointments, has_senior_exec}`。LLM 不出数字，只抽 entity（name + title）。LLM 失败时 `extracted_meta={}` —— 卡里显示 "(姓名待解析)" 占位（bot 路径）或省略抽取块（cron 路径），priority 不因 LLM 失败自动升级（保守语义）。

### ⑫ Form 4 Scanner — Noise vs Signal

**Stage 0 真实数据校准（10 ticker × 30 天 = 89 transactions）**：

| 类型 | Code | 占比 | 含义 | 处理 |
|---|---|---|---|---|
| Noise | A | 44% | Award（RSU / 股权激励） | `noise_summary` 聚合 |
| Noise | M | 21% | Exercise（option vest） | `noise_summary` 聚合 |
| Noise | F | 15% | Tax withhold（RSU vest 税款） | `noise_summary` 聚合 |
| Noise | G | 3%  | Gift（赠予） | `noise_summary` 聚合 |
| Noise | C | <1% | Conversion（衍生品转换） | `noise_summary` 聚合 |
| **Signal** | **P** | 8%  | **Purchase（自掏腰包买入）** | **单卡推送** |
| **Signal** | **S** | 8%  | **Sale（卖出）** | **单卡推送** |

83% 的 Form 4 是 noise（薪酬 / 税务 / 期权操作）—— 这些不是信号。`noise_summary` 写 archive 供未来 Phase 3.5 weekly insider digest 聚合查询，**不单独推送**。只有 P / S 走单卡推送路径。

**Cluster 检测**：同 ticker 同日同方向（purchase / sale）的 P/S 交易，**distinct insider 数 ≥ 3** → 一张 cluster 卡列出所有人 + 总 USD。Stage 0 早期版本用 30 天滚动窗口 → 80% universe 都触发；同日同方向才是真正的协同事件，巧合在 ≥3 个 insider 上极难。

### Priority Ladder

**⑪ 8-K base scores**: `sec_8k_p0=85` / `sec_8k_p1=65` / `sec_8k_p2=55` / `sec_8k_p3=35`

| 触发条件 | 调整 |
|---|---|
| 5.02 LLM 确认 senior_exec（P0 only） | +5 nudge |
| 8-K/A 修正版 | -5 |
| 持仓股 | +15 |
| watchlist 股 | +10 |

**⑫ Form 4 base scores**: `sec_form4_purchase=75` / `sec_form4_sale=50` / `sec_form4_cluster=75`

| 触发条件 | 调整 |
|---|---|
| Purchase ≥ $1M | +25 |
| Purchase $100K-$1M | +10 |
| Purchase by CEO/CFO | +10 |
| Purchase 10b5-1 plan | -10（plan 买入降级，少见但有） |
| Sale ≥ $10M discretionary | +15 |
| Sale ≥ $1M 10b5-1 plan | -5 |
| Cluster ≥ 5 distinct insiders | +15 |
| Cluster 3-4 distinct insiders | +5 |
| Cluster purchase 方向 | +10 |
| 持仓股 / watchlist | +15 / +10 |

**设计哲学**：discretionary sale 比 10b5-1 plan sale 信号强（plan 是几个月前 pre-arranged），cluster purchase 比 cluster sale 信号强（卖股原因多样，集体买入难以解释为巧合）。

### 输出示例

**⑪ HPE 多 item P0 卡：**

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

**⑫ Jensen $2.5M CEO 买入 P0 卡：**

```
📥 内部人买入 · NVDA
申报：2026-06-04 · 交易：2026-06-04
🟢 持仓股

申报人：Jen-Hsun Huang · 🔴 CEO
交易：20,000 股 × $125/股 = $2.50M (discretionary)
```

**⑫ ARM 4 director cluster P0 卡：**

```
📥 内部人集群买入 · ARM
日期：2026-06-04 · 4 笔 / 4 人
🟢 持仓股

总金额：$400.0K

申报人
  • Alice Wong
  • Bob Chen
  • Carol Davis
  • David Lee
```

### Bot 接口

| 命令 | 行为 |
|---|---|
| `/8k AAPL` | 过去 30 天 8-K 摘要 + 5.02 LLM 抽取 |
| `/insiders NVDA` | 过去 90 天（默认）Form 4 摘要：P/S 前 5 笔 + A/M/F/G/C count + 集群 |
| `/insiders NVDA 30` | 自定 days_back，bounded `[7, 365]` |
| NL "AAPL 最近 8-K" | → `eight_k_view` · AAPL |
| NL "NVDA 内部人交易" | → `insider_view` · NVDA |
| NL "AAPL 过去 30 天 insider" | → `insider_view` · AAPL · days_back=30（regex + 中文短语 phrase map）|

### 工程亮点

- **edgartools.Filing.obj() shape 校准**：Stage 0 task 2 实跑确认 `EightK.items` / `Form4.to_dataframe()` 列名（PascalCase `Code` 不是 dataclass field 名 `transaction_code`）—— 早期版本因为列名假设错误漏掉所有 P 信号，Stage 0 测试覆盖回归
- **5.02 senior-exec 升级保守语义**：LLM 失败 → `has_senior_exec` 缺失 → 不自动升 P0。LLM 偶尔出现幻觉的代价是漏 P0 一次（次日 8-K/A 修正版会重推），而不是误升 P0 制造噪音
- **2.02 严格 skip vs 混合保留**：`EightKEvent.is_2_02_only` 判断 `material_codes == {2.02}`（9.01 透明），混合 case 保留卡片避免漏 5.02 重叠的真实材料事件
- **10b5-1 plan 检测**：`form4_parser._is_10b5_1_footnote` 正则匹配 footnote 文本 "10b5-1" / "Rule 10b5-1" / "trading plan adopted on" 等模式，命中即 demote priority。Stage 0 校准结果约 1/3 大额 sale 是 plan
- **5 层 SEC formatter 公共 API**：`format_sec_8k_card` / `format_sec_8k_view` / `format_sec_form4_individual_card` / `format_sec_form4_cluster_card` / `format_sec_form4_view`，source-of-truth 在 `v2/sec/_bot_cards.py`（sandbox-runnable，无 v2.data deps），通过 `v2/reporting/_sec_formatters.py` 4 层 re-export shim 暴露公共 API，cron + bot 共用同函数对象（identity check 测试 pin）
- **byte-equal 跨 surface**：⑪ cron 推送 == `/8k` bot 响应 == `format_sec_8k_*` 的输出对应字节相同（多 fixture × 多 case pin）

### 防幻觉

- LLM 不出数字，**只抽 entity**（5.02 name/title）
- LLM 失败 → **不自动升级**，保守降级到 item parser 决定的 base tier
- **2.02 严格 skip** → 把财报数据完整性交给 ⑧ 的专用 LLM 路径
- 卡片字符串 **HTML 安全 lint**（regex `_UNESCAPED_LT`）强制：bot-facing view formatter 必须 html.escape 所有用户可控字段（ticker / insider name），裸 `<` 必失败

### 测试覆盖（Phase 3 新增）

- **21 个 SEC smoke**（parser + cluster + pipeline + Stage 0 校准回归）
- **21 个 SEC priority integration**（8-K 4-tier × Form 4 各类 + cluster + 10b5-1）
- **15 个 byte-equal pin**（5 个公共 formatter × HPE multi-item / 5.02 LLM-fail / 10b5-1 demoted / cluster 4 / view 各形态）
- **8 个 HTML 安全 lint**（`_UNESCAPED_LT` regex + 多 fixture，bot-facing strict / cron softer trust model）
- **20 个 cron integration**（⑪ 10 + ⑫ 8 + 路由 identity 2，全 sys.modules stub harness）
- **11 个 bot responder**（/8k + /insiders edge cases + days_back bounds + 5.02 LLM-fail placeholder + ticker 校验）
- **架构守护**：regression test 钉死 `v2/bot/responders.py` 不能再出现 inline `_format_*`（Stage 5 lift 完成后永不复活）

总计 96 个 Phase 3 新增测试。

### Phase 3.5 · ⑫b SEC 内部人周报（Fri 19:15 ET）

第十三个 SEC cron 是一个周度聚合任务：每周五 19:15 ET 把本周（Mon-Fri）
所有 ⑫ Form 4 推送从 `archive.pushes` 反查、按 (ticker, direction) 聚合
成一张周报卡片。时段卡在 ⑩ Portfolio Weekly（19:00）和 ⑰ Macro Weekly
（19:30）之间，全周末 operator 复盘动线一气呵成。

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑫b SEC Insider Digest | **Fri 19:15 ET** | 反查本周 ⑫ 推送 → 周报卡 | base **55 (P2)** ；unusual ≥3 tickers 升 P1 |

**数据源 / 简化口径**：Phase 3.5 Stage 0 trace 探测确认 ⑫ archive `trace_json`
只持久化"N signal · Y cluster · Z noise"聚合摘要 —— **per-transaction A/M/F/G/C
breakdown 没有落库**。为避免扩展 archive schema（Phase 3.5 范围控制），⑫b
**纯走 title-only fallback**：正则解析 `Form 4 · NVDA · 买入` 与 `Form 4 集群 ·
ARM · purchase` 两种 title shape，聚合至 (ticker, direction) 粒度。卡片
footer 显式注明这是简化口径，per-code 完整 breakdown defer 到 **Phase 3.5.5**
（届时会加 `form4_transactions` 结构化表）。

**输出示例**：

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

注：基于 ⑫ push title 统计（Phase 3.5 简化口径，
per-code A/M/F/G/C breakdown 见 Phase 3.5.5）
```

**Priority 设计哲学**：P2 floor 同 ⑩⑰ —— operator visibility 第一，**周报
本身不喧宾夺主**。≥3 个 ticker 一周内被 push ≥3 次属于明显的协同行为
（Stage 0 校准 ~17% 周度命中率），升 P1 给出 telegram 即时提醒。空周仍
推一张 "本周 ⑫ 推送平静" 卡片（去 footer 简化口径注释 per UX review），保证
operator 看到 cron 跑了。

### Phase 3.5 测试覆盖

- **20 个 Stage 1 smoke**（`v2/sec/test_phase3_5.py`：ten_q_parser 解析 +
  diff + going_concern/material_weakness 正则 + insider_digest 聚合 + Mon-Fri
  week window）
- **5 个 Stage 3 byte-equal pin**（`format_sec_insider_digest` 4 状态 normal/
  quiet/unusual-3/unusual-overflow + `format_earnings_summary` 含 10-Q section）
- **10 个 Stage 4 cron integration**（`TestInsiderDigestCron` 5：archive
  反查 → 周报卡 + responder framing + byte-equal；`TestEarningsTenQIntegration`
  5：MD&A section render + going_concern P0 + material_weakness +15 reason +
  silent skip）

总计 35 个 Phase 3.5 新增测试。其中 Stage 4 cron integration 抓出 Stage 2
的 silent-ship bug —— priority.py 加了 `+20 going_concern_in_10q` 规则但
`scripts/earnings_summaries.py::_push_summarized` 没 forward 进 metadata，
end-to-end 测试是唯一防御。

---

## ⑭⑮⑯⑰ Macro Agent · 宏观数据 + FOMC

第十四到第十七个 agent 是 Phase 4 的四个 macro cron：⑭ 每个交易日 16:30 ET 推送 VIX / DXY / 收益率 daily snapshot 并按 anomaly flag 升级，⑮ 09:00 ET 在 release_calendar 命中日 触发 CPI/PCE/NFP/GDP/PPI 单 release 卡（FOMC 走专用 Layer 3 路径），⑯ 周四 09:30 ET 推 Initial Claims，⑰ 周五 19:30 ET 推 macro weekly recap。同时暴露 `/macro` `/cpi` `/fomc` `/yields` 4 个 bot 命令做 on-demand 查询，跨 surface 公用同一组 6 个公共 formatter 保证 byte-equal。

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑭ Daily Snapshot | Mon-Fri **16:30 ET** | VIX/yields/大宗实时 + 4 个 anomaly flag | 见 ladder |
| ⑮ Release Scanner | Mon-Fri **09:00 ET** | release_calendar 命中日的 CPI/PCE/NFP/GDP/PPI/FOMC | σ ladder |
| ⑯ Initial Claims | **Thu 09:30 ET** | 周度 ICSA + 4W MA smoothed level | base P2, σ>2 升 P1 |
| ⑰ Macro Weekly | **Fri 19:30 ET** | 本周已发布 + 下周预告 + 周内 VIX/yields 变化 | 固定 P1 (floor) |

### 数据源 — hybrid 策略

**Layer 优先级 + 单点失败 silent skip per field**：

| 用途 | 源 | 备注 |
|---|---|---|
| Treasury yields (2Y/10Y/T10Y2Y/T10Y3M) + Fed Funds Upper/Lower | **FRED** canonical EOD | 避开 yfinance `^TNX` / `^FVX` / `^TYX` 返回 × 10 raw 值的坑 |
| CPI/PCE/NFP/GDP/PPI/Claims series | FRED canonical | 含 vintage_dates 处理 prelim → final 修正 |
| VIX 实时 + DXY + WTI + Gold | **yfinance** (15min 延迟) | EOD canonical 由 FRED `VIXCLS` 兜底 |
| FOMC sell-side aggregate | **Tavily** (8 trusted domains) | 替代 LLM 鹰鸽判断（Layer 3 防御）|
| Release 解读 bull/bear/narrative/tone | **DeepSeek** template-fill (Layer 1+2) | 数字全 Python，LLM 只出 label |

**实现细节**: `fredapi.Fred.get_series` 走 fredapi wrapper；`get_release_dates` fredapi 不支持，直接 httpx 调 `https://api.stlouisfed.org/fred/release/dates` REST endpoint (Stage 1 prod-box 跑 `_seed_calendar` 时发现的 bug，patch 用 3 次 backoff 重试)。

### release_calendar.py · 硬编码 2026 schedule

**为什么硬编码而不实时拉**：cron 路径必须 deterministic + 离线可跑，BLS/BEA 的官方 schedule 一年只变几次。`_seed_calendar.py` 一次性脚本用 FRED `/release/dates` REST API 生成 40 个 release entries 覆盖 33 个 dates（CPI/PCE/NFP/GDP/PPI 各 7 + FOMC × 5）粘贴到 release_calendar.py 的 `AUTOGEN BLOCK START/END` sentinel 之间。`_LAST_UPDATED` 字段 + 6 个月 staleness check 在 cron 启动时 warn——operator 看到 warning 重跑 `_seed_calendar.py` 更新 dict 即可。

**Claims (ICSA) 不进 calendar**: 周度 deterministic Thursday 触发，⑯ cron 用 `CronTrigger(day_of_week="thu")` gate 直接跑 `build_claims_event`，不查 calendar。

### LLM 4 层防御（Phase 4 核心架构）

| 层 | 位置 | 防御 |
|---|---|---|
| **L1 Template-Fill** | `summarizer._SYSTEM_PROMPT` | 严禁输出数字 + 严禁预测后市方向 + 4 个固定 JSON 字段（bull/bear/narrative/tone，每个 ≤40 字符） |
| **L2 Post-Parse Reject** | `summarizer._REJECT_PATTERNS` + `_DIGIT_LEAK_RE` | 正则扫描 "将/会/预期/预计/料/可能.*[上下涨跌升降]" + 数字泄漏（带 % / bps / 个百分点 后缀）→ fallback to neutral |
| **L3 FOMC bypass** | `fomc_parser` + `tavily_consensus` | 鹰鸽判断完全不调 LLM——Python diff 15 个 KEY_PHRASES + SEP dot plot 数字 extract + Tavily majority vote 8 trusted domains |
| **L4 Historical analog** | Phase 4.5 deferral | "类似 surprise 历史上 N 天后市场反应" 待研究，目前缺；当前实现承认 release 已发生，不预测后市 |

**保守降级**: LLM 失败 (timeout / parse fail / Layer 2 reject) → 返回 `_NEUTRAL_FALLBACK` (`tone="neutral"`, `narrative="数据已发布，详见上方数值。"`)。Priority 不因 LLM 失败漏掉 σ 升级——sigma 是 Python 算的。

### ⑭ Daily Snapshot — Anomaly 升级

**Base scores**: `macro_snapshot_p3=35` / `macro_vix_spike=85` / `macro_curve_flip=65`

| 触发条件 | 调整 | kind |
|---|---|---|
| VIX 单日 ≥ +30% (extreme) | +20 | `macro_vix_spike` (P0) |
| VIX 单日 ≥ +20% (strong) | +10 | `macro_vix_spike` (P0) |
| T10Y2Y 由正转负今日翻转 | +10 (yield_curve_inverted) | `macro_curve_flip` (P1) |
| VIX 单日 ≥ +10% 偏高 OR DGS10 单日 \|Δ\| ≥ 20bps shock | (highest-priority anomaly routes through `macro_curve_flip`) | P1 |
| 正常日（无 anomaly） | 仅 base 35 | `macro_snapshot_p3` (P3, 日常 ambient 推送) |

**Snapshot 自分类**: `_classify(snap)` 按 `vix_spike → curve_flip → vix_elevated/rates_shocked → default` 优先级选 kind。卡片自身 icon (`🚨`/`📉`/`📊`) 由 formatter 看 `snap.vix_spike/curve_flip/...` flags 自适应——cron 不传 icon hint。

### ⑮ Release Scanner — σ ladder

**Base scores**: `macro_release_p2=55` / `macro_release_p1=65` / `macro_release_p0=85`

| 条件 | 调整 | tier |
|---|---|---|
| `\|surprise_sigma\|` ≥ 3.0 extreme | +20 | P0 (`macro_release_p0`) |
| `\|surprise_sigma\|` ≥ 2.0 big | +10 | P1 (`macro_release_p1`) |
| `\|surprise_sigma\|` ≥ 1.0 moderate | +5 | P1 nudge |
| FOMC + SEP `hawkish_shift` / `dovish_shift` | +15 (`sep_*_shift`) | P0 boost |
| Tavily 卖方读数 hawkish_unexpected | +10 (`sell_side_hawkish`) | P0/P1 nudge |

**FOMC 路径**: σ ladder 不适用（FOMC 没有"surprise σ"——市场 priced-in 程度太杂）。`_fomc_kind_and_meta` 看 `event.sep_dot_plot_change`：`hawkish_shift` / `dovish_shift` 升 `macro_release_p0`；`no_change` 走 `macro_release_p1` base 65（FOMC 永远至少 P1）。

### ⑯ Initial Claims — 简化路径

ICSA 每周四 08:30 ET 发布，cron 09:30 ET 跑（给 FRED 1 小时缓冲）。`build_claims_event` 拉 FRED ICSA series → headline = latest weekly, core = 4-week MA smoothed。`σ` 用 historical std 算。

**Base**: `macro_release_p2=55` (P2), `\|σ\| ≥ 2` → 升 `macro_release_p1=65` + `big_surprise` nudge → P1。假期周 (Thanksgiving / 跨年) FRED 没数据 → `build_claims_event` 返回 None → cron silent skip + log INFO。

### ⑰ Weekly Recap — operator visibility floor

**Base**: `macro_weekly=65` → 永远 P1（同 ⑩ Portfolio Weekly 的 floor 哲学：周报 operator 必须看见，不靠事件驱动）。

**时间冲突**: 19:30 ET 是 Stage 0 spec 关键的时段——错开 ⑨ 18:30 (Portfolio Risk) + ⑩ 19:00 (Portfolio Weekly)，与 ⑧ 21:00 (Earnings Summaries) 也有 90 分钟缓冲。

**卡片内容**: 本周 already-fired releases（从 `release_calendar.get_releases_in_window(week_start, today_iso)`）+ 下周 schedule preview + 周内 4 个核心 series 变化（VIX 用 pts 单位，DGS10/DGS2/T10Y2Y 用 bps 单位——industry standard）。

### Bot 接口

| 命令 | 行为 |
|---|---|
| `/macro` | 综合 dashboard：market 状态 + 收益率（含 10-2 spread bps）+ 最近 3 个 release + 下次 5 个 release |
| `/cpi` | 最新 CPI release 卡（含 surprise σ + LLM Layer 1+2 输出）|
| `/fomc` | 最近 FOMC：statement diff + SEP dot plot（如有） + Tavily 卖方读数 + 下次 FOMC 预告 |
| `/yields` | 实时收益率曲线（目前复用 macro_view dashboard yields 区块） |
| NL "宏观怎么样" | → `macro_view` |
| NL "最近 CPI 数据" | → `release_check` · release_type=cpi |
| NL "上次 FOMC 怎么说" | → `release_check` · release_type=fomc |
| NL "NFP data this month" | → `release_check` · release_type=nfp |

**read-only 语义**: 同 `/risk` `/8k` `/insiders` — bot 路径不写 archive、不算 priority、不发 alert。

### 工程亮点

- **fredapi REST patch**: `_seed_calendar.py` 在 prod box 首跑暴露 `'Fred' object has no attribute 'get_release_dates'` bug——fredapi 0.5.2 wrapper 没暴露该方法。Patch 用 httpx 直接调 FRED REST `/fred/release/dates` endpoint，3 次 1s linear-backoff retry。35 → 40 smoke tests（+5 包括 dependency-surface guard，未来 fredapi 升级若加回该方法会 surface）。
- **6 层 macro formatter 公共 API**：`format_macro_daily_snapshot` / `format_macro_release_card` / `format_macro_fomc_card` / `format_macro_claims_card` / `format_macro_weekly_recap` / `format_macro_dashboard`，source-of-truth 在 `v2/macro/_bot_cards.py`（sandbox-runnable，无 v2.data deps），通过 `v2/reporting/_macro_formatters.py` 4 层 re-export shim 暴露公共 API，cron + bot 共用同函数对象（identity check 测试 pin）。
- **Stage 5 UX polish**：⑭ snapshot 10Y-2Y 改 bps（行业标准，与 dashboard 一致）；⑮ release Consensus 改 pct（跟 MoM/YoY 一致，从 raw `0.00` 修到 `+0.30%`）；⑭ markets/rates 之间加空行（与 dashboard 对称）；⑰ weekly recap rates 改 bps，VIX 改 pts。
- **byte-equal 跨 surface**：⑭⑮⑯⑰ cron 推送 == `/macro` `/cpi` `/fomc` `/yields` bot 响应 == `format_macro_*` 输出字节相同（16 个 byte-equal pin tests 覆盖 normal / spike / curve_flip / fallback / hawkish_shift / 等各种场景）。
- **Layer 3 FOMC 路径**：fomc_parser.py 用 Python 对比 15 个 KEY_PHRASES（"appropriate firming" / "modest" / "elevated" / "longer-run" / 等）的 added/removed，extract_dot_plot_table 用 regex 提取 Federal funds rate 行 + 年份头，classify_sep_shift 比较 current vs prior median 决定 hawkish_shift/dovish_shift/no_change/mixed。Tavily 走 8 trusted domains (Reuters/Bloomberg/FT/WSJ/CNBC/SeekingAlpha/MarketWatch/federalreserve.gov) 抓搜索结果，简单 keyword count 决定 majority 标签。**LLM 完全不参与鹰鸽判断**。

### 测试覆盖 (Phase 4 新增)

- **40 个 macro smoke** (transforms / calendar / summarizer L1+L2 / FOMC parser / pipeline / fred REST path / fingerprint registration)
- **16 个 priority ladder integration** (7 base scores 注册 + σ ladder × 3 + FOMC SEP shift × 3 + sell-side × 1 + vix_spike × 3 + curve_flip × 1 + weekly + snapshot_p3)
- **16 个 byte-equal pin** (3 snapshot + 3 release + 2 FOMC + 1 claims + 2 weekly + 3 dashboard + 2 surface/identity)
- **9 个 HTML 安全 lint** (`_UNESCAPED_LT` regex + hostile fixtures w/ `<script>` 在 LLM bull/bear/narrative / Tavily sources / FOMC diff phrases)
- **9 个 bot responder integration** (macro_view full / partial / spike / snapshot exception graceful + release_check CPI / FOMC / invalid type / no calendar / FRED failure)
- **22 个 cron integration** (5 class — Snapshot × 6 / Release × 8 / Claims × 3 / Weekly × 3 / Architecture Guard × 2)
- **架构守护**: regression test 钉死 `scripts/macro_*.py` + `v2/bot/responders.py` 不能再出现 inline `_format_macro_*` / `_format_snapshot_*` / `_format_release_*` 等 9 个 forbidden 函数定义，且所有 cron 必须走 `from v2.reporting import` 公共 API。

总计 112 个 Phase 4 新增测试。

### Phase 4.5（推迟项目）

- **FOMC press conference transcript +6h follow-up**: ⑮b cron, 用 Tavily 抓 Powell 发布会 transcript chunk + summarize Q&A 重点，再推一张 follow-up 卡片。Stage 0 design 时即决定 defer，需独立 source/chunking/audio→text 设计。
- **Historical analog mode**: "类似 surprise 历史上 N 天后市场反应" RAG 检索，需要 ChromaDB 装 macro release 历史 embedding。
- **自动 release_calendar scraping**: 目前每年初手动 rerun `_seed_calendar.py`；可加 monthly cron 自动调 FRED + Fed announcement HTML scraping 校验。

---

## 板块相对强度对比

每条信号（筛选 / 盘后异动 / 盘中异动）在被推送之前都和板块 ETF 做相对强度对比。`NVDA +5% 量爆`这条信号在 `SMH +4.5%` 时几乎是 beta，在 `SMH -1%` 时才是真正的 ticker-specific catalyst。

| Ticker 类别 | 对应板块 ETF |
|---|---|
| 半导体（NVDA, AMD, AVGO, QCOM, INTC, TXN, MU）| SMH |
| 大盘科技 + 软件 + 互联网（AAPL, MSFT, GOOGL, META, ORCL, CRM, ADBE, …）| XLK |
| 默认兜底 | SPY |

板块 ETF 的价格序列在每次 agent 启动时一次性预取（全 universe 加 3 个额外 FD 调用）。`contrarian` flag 仅在 ticker 与板块**反向**且差距 ≥1.5pp 时触发——只有此时 `★ 逆势` chip 才出现。

**生产环境真实案例**：`MU +3.63%、SMH -1.10%、1.5× 量 → ★ 逆势 chip + Tier-1 归因独立 surface「MU 市值突破 $1T」作为 ticker-specific catalyst`。信号与解释相互验证。

---

## 数据源

| 数据源 | 提供什么 | 为什么选这个 |
|---|---|---|
| **yfinance**（默认 daily prices，Phase 4.5-mini 后）| Daily OHLCV (`get_prices`) for ① 筛选 / ② 异动 / ③ 横向扩展 / `/why` `/summary` bot 路径 | 实时 EOD，无 FD free tier 1-3 天滞后；卡片日期立刻显示 today 而不是 today-3 |
| financialdatasets.ai | 财报历史（`get_earnings`）/ 内部人交易（`get_insider_trades`）/ 公司基本面（`get_financial_metrics` / `get_company_info`）| 季度 / 离散事件型数据，无每日 lag 问题；主商业 API；schema 一致 |
| yfinance（其他用途）| 期权链（OI + volume）+ Phase 4 macro VIX/DXY/WTI/Gold | 免费；驱动"期权异动检测器" + macro snapshot |
| SEC EDGAR（通过 `edgartools`） | 13F-HR 机构持仓 filing + 8-K / Form 4 / 10-Q 监控 | 权威源 |
| ARK Invest CDN | ETF 每日持仓 CSV | 公开免费；日级别粒度 |
| Alpaca（paper） | 实时价格（streamer）、账户持仓 + P&L | 免费；驱动 `/portfolio`、`/pnl`、streamer alerts、portfolio risk cron |
| FRED (Federal Reserve Economic Data) | 宏观时间序列（CPI/PCE/NFP/GDP/PPI/Treasury yields/Fed Funds）| 权威 EOD 源；含 vintage 处理；避开 yfinance `^TNX` × 10 raw bug |
| Tavily News API | 归因 + 验证用的新闻搜索 + FOMC sell-side aggregate | 搜索引擎级别质量；Layer 3 防御替代 LLM 鹰鸽判断 |
| DeepSeek (`deepseek-chat`) | 生成 + 验证 + 意图分类 | 中英双语下的性价比最高 |
| OpenAI `text-embedding-3-small` | RAG 记忆 embedding | 便宜准确，小维度 |

**Phase 4.5-mini 价格源解耦**（commit `b36d5e3` 系列）：`v2/data/price_source.py` 暴露 `PriceSource` Protocol + `YFinancePriceSource`（默认）+ `FDPriceSource`（backtest / event-study 仍用，保 reproducibility）。Ops 通过 `V2_PRICE_SOURCE=fd` env var 可一键 fallback 回 FD（无需重新部署）。FD 客户端仍 source-of-truth 处理非时间敏感数据。

---

## 技术栈

- **Python 3.11** + Poetry
- **LangChain** + `langchain-deepseek` 做 LLM 编排
- **APScheduler**，`BlockingScheduler` + `US/Eastern` cron 触发器
- **python-telegram-bot**（异步、polling 模式）
- **alpaca-py** 实时价格 + paper 账户访问
- **SQLite** WAL 模式，多服务并发读写
- **ChromaDB** 向量化长期记忆
- **edgartools** 解析 SEC filing
- **matplotlib** + Noto Sans CJK 渲染中文图表
- **systemd** Ubuntu 24.04 上做服务守护

---

## 项目结构

```
v2/
├── data/             # FDClient + CachedFDClient + NewsProvider Protocol
├── screening/        # ① 基本面筛选 + delta 增强 + 叙述器
├── monitoring/       # ② 检测器 + 多源归因 + Verifier
├── lateral/          # ③ LLM 扩展 + Tavily 关系验证
├── institutional/    # ④ EDGAR 客户端 + CUSIP 聚合 + 变动
├── etf/              # ⑤ ARK CSV 客户端 + 每日 snapshot + diff
├── earnings/         # ⑦⑧ yfinance 日历 + FD 历史 + LLM 总结 + 卡片
├── portfolio/        # ⑨⑩ Alpaca 持仓 → RiskReport（集中度 / 暴露 / P&L / 回撤）
├── sec/              # ⑪⑫ edgartools wrapper + 8-K item parser + Form 4 + cluster + 5.02 NER
├── macro/            # ⑭⑮⑯⑰ FRED + yfinance + Tavily + LLM template-fill + FOMC fomc_parser
├── streamer/         # 盘中 runner + universe 扫描器
├── broker/           # Alpaca paper 账户适配器（只读，+get_portfolio_history）
├── universe/         # TECH_30 + 90 ticker → 行业 ETF 映射（含 OTHER 桶）
├── reporting/        # Telegram 格式化器 + notifier + matplotlib
├── memory/           # ChromaDB 驱动的 AnomalyMemory
├── archive/          # 每条推送的 SQLite 日志（离线查询 + RAG）
├── bot/              # Telegram bot：命令、意图分类、响应器、状态
└── scheduler/        # APScheduler 配置 + 子进程 job

scripts/
├── run_scheduler.py             # Scheduler 入口
├── run_telegram_bot.py          # Bot 入口
├── run_streamer.py              # Streamer 入口（--test-now 强制单次扫描）
├── daily_screen_to_telegram.py  # ①
├── anomaly_to_telegram.py       # ②
├── lateral_to_telegram.py       # ③
├── institutional_to_telegram.py # ④
├── backfill_13f.py              # ④b
├── etf_daily_snapshot.py        # ⑤
├── earnings_reminders.py        # ⑦
├── earnings_summaries.py        # ⑧
├── portfolio_risk_to_telegram.py    # ⑨
├── portfolio_weekly_to_telegram.py  # ⑩
├── sec_8k_to_telegram.py            # ⑪
├── sec_form4_to_telegram.py         # ⑫
├── sec_insider_digest_to_telegram.py # ⑫b
├── macro_daily_snapshot.py          # ⑭
├── macro_release_to_telegram.py     # ⑮
├── macro_claims_to_telegram.py      # ⑯
└── macro_weekly_to_telegram.py      # ⑰
```

---

## 快速开始

### 前置依赖

- Python 3.11
- Poetry
- API keys：DeepSeek、financialdatasets.ai、Tavily、OpenAI、Telegram bot token、Alpaca（paper）

### 安装

```bash
git clone <repo-url> hedge-fund
cd hedge-fund
poetry install --no-root

cp .env.example .env
# 填入：
#   DEEPSEEK_API_KEY, FINANCIAL_DATASETS_API_KEY, TAVILY_API_KEY,
#   OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#   APCA_API_KEY_ID, APCA_API_SECRET_KEY   （Alpaca — paper 账户免费）
```

### 冒烟测试单个 agent

```bash
poetry run python scripts/daily_screen_to_telegram.py
```

### 前台跑 scheduler

```bash
poetry run python scripts/run_scheduler.py
# 可选：--test  把每个 job 跑一次再退出
```

### 跑 bot

```bash
poetry run python scripts/run_telegram_bot.py
```

### 跑 streamer

```bash
poetry run python scripts/run_streamer.py
# 可选：--test-now  绕过市场时段检查强制跑一次扫描，
#                  结果推到 Telegram 后退出
```

---

## 部署（Ubuntu 24.04 + systemd）

三个服务并发跑。示例 unit 文件：

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

```ini
# /etc/systemd/system/hedge-fund-bot.service
[Unit]
Description=AI Hedge Fund Telegram Bot
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hedge-fund
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/root/hedge-fund"
ExecStart=/root/.local/bin/poetry run python scripts/run_telegram_bot.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/root/hedge-fund/logs/bot.log
StandardError=append:/root/hedge-fund/logs/bot.err

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/hedge-fund-streamer.service
[Unit]
Description=AI Hedge Fund Alert Streamer
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hedge-fund
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/root/hedge-fund"
ExecStart=/root/.local/bin/poetry run python scripts/run_streamer.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/root/hedge-fund/logs/streamer.log
StandardError=append:/root/hedge-fund/logs/streamer.err

[Install]
WantedBy=multi-user.target
```

三个服务合计常驻内存：scheduler ~205 MB、bot ~50 MB、streamer ~80 MB。**1 GB 的 DigitalOcean droplet 跑得很轻松**。

---

## 值得讲的工程亮点

### Push vs Pull 语义分离

`/13f BRK` 永远返回最新已知的组合，无论 scheduler 之前是否推过同一份 filing。**cron 路径会和 `edgar.db` 去重以避免重复推送**；**bot 路径完全跳过这个检查**。一个底层 pipeline 两套调用接口，对应两种访问模式。

### 13F 解析的 CUSIP 聚合

Berkshire 通过三个子公司持 AAPL（BHRG、GEICO、National Indemnity）。13F-HR 信息表把它们记成三行 **相同 CUSIP** 的独立条目。我们的 PK 是 `(accession, cusip)`，**naive 的 `INSERT OR REPLACE` 会无声地丢掉其中两条**。修复方式：在 EDGAR parser 层做聚合——一个 CUSIP 一行，shares 和 market value 求和。**Berkshire 的 AAPL 持仓从错误的 $958M 修正为正确的 $57.8B（组合占比 22%）**。

### 严格枚举意图分类

NL 分类器输出 15 个意图之一加上参数。输出是 JSON、被解析、被白名单 set 校验。任何不合法的都变成 `unknown`。**LLM 永远不决定要"说"什么，只决定要"调用"哪个工具**。如果用户想让 DeepSeek 凭想象写金融分析，他们可以直接问 DeepSeek。这个系统存在的意义是 grounded、多源、可验证。

### 原子触发的 Alert

Streamer 的 `alert_fire_check` 跑的是 `UPDATE alerts SET fired_at=? WHERE id=? AND fired_at IS NULL`。`fired_at IS NULL` 这个守卫让操作在并发 streamer 实例下也是幂等的——即使两个 poller 同时抢同一条 alert，**也只有第一个能成功 UPDATE**。one-shot 语义在 SQL 层而不是应用层强制。

### Streamer 上的 Push / Pull 也是分离的

用户 `/alert` 和 TECH_30 自动扫描是两条独立路径，共享 streamer 主循环。一个失败不会压制另一个——`_check_user_alerts` 和 `scan_universe` 各自 try/except 包住。**Streamer 永远不会因为一只异常 ticker 而死掉**，最坏情况是那只 ticker 这一分钟被静默跳过。

### 板块基准对比作为信号质量护城河

每个 agent run 启动时预取 3 个 ETF 序列（SPY/XLK/SMH），让每条异动都自带相对强度对比，**而且不需要烧 LLM token**。`★ 逆势` chip 把 *NVDA +3% 但 SMH -1%* 这种真信号从 *AAPL +1.2% 在 +1.0% 大盘日* 这种 beta 噪音里挑出来。

### 异步安全的长任务

像 `/why NVDA` 这种 20 秒级 pipeline（FD + Tavily + DeepSeek × 2），通过 `asyncio.get_running_loop().run_in_executor(None, sync_fn, *args)` 投递到默认 executor。**Polling 循环保持响应**，其他用户的命令不会被阻塞（单用户场景里其实无所谓，但模式是对的）。

### 信源 tier 防御

Verifier prompt 显式列出 Tier 1/2/3 domain 清单。Generator 只能看到 Tier 1 + Tier 2 来源。**不让推测站污染归因是设计上的明确选择**——**静默丢弃比自信乱讲更重要**。

### 过期数据 = 数据完整性问题

Greenlight Capital 最新 13F 是 2023-Q4。他们可能已经跌破 $100M 申报门槛。**不打 ⚠️ chip 就直接显示这份数据，等于无声地把 29 个月前的仓位当作 Einhorn 的当前状态**——这是数据完整性问题，不是 UX 问题。chip 把时间边界显式呈现给用户。

### 盘中 Streamer 刻意不调 LLM

交易时段里，streamer 每分钟扫描 TECH_30 找双门槛异动并推简化卡。**它不调 Tavily、不调 DeepSeek、甚至连新闻层都不拉**。理由：盘中新闻滞后于价格。如果 10:15 触发归因，模型其实没有真实素材可用，**只会瞎编**。盘中卡片显式指向 `/why TICKER` 并标明深度归因在 17:35 ET 才可用。

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

`CachedFDClient` 层通过 per-endpoint TTL SQLite 缓存（基本面 24h、价格 6h、公司信息 7d；新闻刻意不缓存）**消除了约 65% 的 FD 调用**。

盘中 streamer 在交易时段每分钟新增 ~30 个 Alpaca 调用（一次批量 latest-trade + 一次批量 daily-bars 覆盖全部 32 个跟踪标的），远远低于免费层 200/min 的限制。

---

## Roadmap

### 已完成 ✅

- **Phase 0 · Push priority 系统** —— `v2/reporting/priority.py` 4 层 P0/P1/P2/P3，importance_score 与 archive.db 同步落地，P2 日 16:45 ET 汇总 cron
- **Phase 1 · Earnings Agent（⑦⑧ + /earnings）** —— yfinance 日历 + FD actual/estimate + LLM 总结 + Tavily transcript + 跨季度去重
- **Phase 2 · Portfolio Risk Agent（⑨⑩ + /risk + /pnl extension）** —— 详见上一节；含 4 个公共 formatter + 28 个新测试 + drawdown 5 层符号防御
- **Phase 2.5-mini · BROAD-market ETF bucket** —— IVV/SPY/VOO/QQQ classification
- **Phase 2.5 完整版 · Per-position attribution + drawdown realtime fix** —— ⑨b 新 sub-cron (Mon-Fri 16:25 ET) 静默写入 `positions_snapshot` 表；⑩ Friday 19:00 ET 读 7 天窗口 + Brinson-style attribution (`contribution = avg_weight × weekly_return`) → 卡片含最佳/最差/净贡献；`compute_drawdown` 加 `today_realtime_value` 参数, fix 2026-06-05 prod-bug (`今日 -3.72%` / `drawdown 0.00%` 矛盾)；scheduler 18→19 jobs；20 个 snapshot smoke + 4 个 attribution + drawdown 端到端 cron integration + 6 个 ⑨b cron integration。无新 deps。
- **Phase 3 · SEC 监控 Agent（⑪⑫ + /8k + /insiders）** —— 详见上一节；含 5 个公共 formatter + 96 个新测试 + 24-item 8-K priority 表 + Form 4 noise/signal 区分 + 同日 ≥3 cluster 检测 + 5.02 LLM NER + HTML 安全 lint
- **Phase 3.5 · 10-Q parser + ⑫b SEC 内部人周报** —— 详见上一节；含 1 新 formatter (`format_sec_insider_digest`) + 35 个新测试 + ⑧ 卡片末尾追加 "📋 10-Q MD&A 关键变化"（段落 cap 80 字×3 + new RF count + going_concern P0 / material_weakness +15）+ ⑫b Fri 19:15 ET title-only 周度聚合（P2 floor / 异常活跃 ≥3 tickers 升 P1）+ scheduler 19→20。Stage 4 integration test 抓出 Stage 2 silent-ship bug（priority 规则未 forward）。Per-code A/M/F/G/C breakdown defer 到 Phase 3.5.5
- **Phase 4 · Macro Agent（⑭⑮⑯⑰ + /macro + /cpi + /fomc + /yields）** —— 详见上一节；含 6 个公共 formatter + 112 个新测试 + FRED+yfinance hybrid 数据源 + LLM 4 层防御（Template-Fill / Post-Parse Reject / FOMC Layer 3 no-LLM-verdict / Historical analog 待 Phase 4.5）+ 硬编码 release_calendar + bps 行业标准格式
- **Phase 4.5-mini · Daily prices migration FD → yfinance** —— `v2/data/price_source.py` 暴露 `PriceSource` Protocol + `YFinancePriceSource`（默认）+ `FDPriceSource`（backtest 仍用）+ `default_price_source()` factory（`V2_PRICE_SOURCE=fd` env var ops 一键 fallback）。消除 Phase 1-4 时 `fd_safe_today()` 强制的 `today - 3` buffer：①/②/③/`/why`/`/summary` 卡片日期现在显示 today 而不是 today - 3。`v2/data_safety.py` 整模块删除；8 callers (5 v2 modules + 3 cron scripts) + 1 utility 全部迁移；12 个 price_source smoke test。FD client 仍 source-of-truth 处理 earnings / insider / financials。
- **Web 仪表盘** —— FastAPI + React + Tailwind，覆盖 `archive.db` 自动推送 feed、trace 回放、用户问答 mode
- **观测层 SDK** —— `v2/observability/` 对 FD / DeepSeek / Tavily / 意图分类器做 monkey-patch，所有 agent 自动产 trace
- **439 个 sandbox 单元测试** —— Phase 0-4 + 4.5-mini + 2.5 完整版 + 3.5 累计：Phase 1 (21 earnings byte-equal + 17 earnings priority + 9 earnings pipeline + 10 earnings cron integration) + Phase 2 (12 portfolio formatter byte-equal + 19 portfolio priority + 5 priority floor + 10 portfolio pipeline edges + 17 portfolio cron integration + 20 portfolio smoke) + Phase 2.5 完整版 (20 snapshot/attribution/drawdown smoke + 6 ⑨b cron integration) + Phase 3 (21 SEC smoke + 21 SEC priority + 15 SEC byte-equal + 8 HTML safety + 20 cron integration + 11 bot responder) + Phase 3.5 (20 Stage 1 smoke + 5 Stage 3 byte-equal + 10 Stage 4 cron integration) + Phase 4 (40 macro smoke + 16 macro priority ladder + 16 macro byte-equal + 9 HTML safety + 9 bot responder + 22 cron integration) + Phase 4.5-mini (12 price_source smoke) + 通用 (9 archive migration + 33 intent classify + 18 priority + 13 observability + 余下分布)

### 进行中 / TODO

- **Phase 3.5.5 (optional)** —— `form4_transactions` 结构化表（替代 ⑫b 当前的 title-only 简化口径）：per-A/M/F/G/C 完整 breakdown + 聚合 USD 总额 + insider-name 维度的趋势聚合
- **Phase 4.5 (optional)** —— FOMC press conference transcript +6h follow-up cron (Tavily 抓发布会 Q&A 重点) / Historical analog mode (类似 surprise 历史上 N 天后市场反应 RAG) / 自动 release_calendar scraping (替代每年初手动 rerun `_seed_calendar.py`)
- **Phase 5 · Market Regime + ARK 显著调仓告警**
- **Phase 6 · News 重构 + 3-tier Universe**
- 持续：Migration system audit
- GitHub Actions CI（pytest 在 main 上跑）
- 多用户支持（每个 chat 独立 watchlist + 持仓）
- 接入现有 v2 event-study 框架做 backtest
- Alpaca 从 paper 切换到 live（paper 充分验证后改一个配置开关）

---

## 致谢

仓库外壳和原始教育版 `app/` 目录来自 [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)——一个开源 AI hedge fund 概念项目。`v2/` 目录——也就是本 README 描述的整个另类数据 agent 系统（六个盘后 agent + 两个财报 cron + 两个组合风险 cron + 三个 SEC 监控 cron（含 ⑫b 周度内部人摘要）+ 四个 Macro Agent cron、盘中 Streamer、含 23 个 NL intent 的 Telegram bot、5 层防幻觉机制、推送优先级系统、所有 SQLite + ChromaDB 存储、所有 systemd 部署脚手架）——是在那个基础上**完全从零搭建的独立项目**。

## License

MIT。教育用途。**不构成投资建议**。
