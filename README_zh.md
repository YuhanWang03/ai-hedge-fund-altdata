# AI Hedge Fund · 另类数据 Agent 系统

一个生产级的另类数据情报平台，以 Telegram bot 作为交付界面。**六个盘后批处理 agent** 持续监控美股市场，**两个财报 cron** 自动追踪 watchlist + 持仓的 D-3/D-1/D-0 提醒和发布后总结，**两个组合风险 cron** 推送每日集中度 / P&L / 回撤快照 + 周度复盘，**一个盘中 Streamer 服务**实时扫描异动并触发用户价格提醒，**十九个自然语言意图**处理交互式查询，**五重防幻觉机制**保证每一条推送都有可追溯的源头。

定位为简历展示型项目，旨在展现端到端拥有"多源数据管道 + 双 LLM 校验架构 + 7×24 部署系统"的能力。

[English README](./README.md)

---

## 它能做什么

系统在一台 $6/月 的 VPS 上并发运行三个服务：

**Scheduler（调度器）** —— 十二个 cron 任务，覆盖基本面筛选、异动检测、产业链横向扩展、机构 13F 跟踪、周度回填、ETF 每日持仓抓取、财报日历提醒、财报发布后总结、组合风险快照、周度复盘、P2 日汇总、archive 清扫。每个任务进程隔离，单个崩溃不会污染其他。

**Streamer（实时扫描）** —— 分钟级盘中扫描器。在美股交易时段（9:30–16:00 ET）轮询 Alpaca 实时行情，做两件事：检查用户设置的价格提醒是否触发；扫描 TECH_30 universe 寻找双门槛异动（≥3% 价格变动 **且** ≥2.5× 成交量节奏）。**盘中绝对不调用 LLM / Tavily** —— 深度归因留给 17:35 ET 的盘后 agent。

**Telegram Bot** —— 24/7 长轮询机器人，支持 19 个 slash 命令、3 个 watchlist 命令，以及一个含 19 种意图的自然语言分类器。通过 chat ID 过滤实现单用户授权。

三个服务通过 WAL 模式共享 7 个 SQLite 数据库 + 一个 ChromaDB 向量库（用于 RAG 记忆）。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-scheduler.service                 │
├──────────────────────────────────────────────────────────────────┤
│  08:00 ET 周一-周五  ⑦ 财报日历提醒        → Telegram 推送       │
│  17:00 ET 周一-周五  ⑤ ETF 每日持仓        （静默写入）          │
│  17:30 ET 周一-周五  ① 基本面筛选          → Telegram 推送       │
│  17:35 ET 周一-周五  ② 异动监测            → Telegram 推送       │
│  18:00 ET 周一       ③ 产业链横向扩展      → Telegram 推送       │
│  18:00 ET 周二/周五  ④ 机构 13F             → Telegram 推送       │
│  18:30 ET 周一-周五  ⑨ 组合风险快照        → Telegram 推送       │
│  18:30 ET 周日       ④b 13F 周度回填        （静默刷新）         │
│  19:00 ET 周五       ⑩ 组合周度复盘        → Telegram 推送       │
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
│                   /portfolio /pnl /settings                      │
│                   /watchlist /add /remove                        │
│  NL 分类器      · 15 个意图 · DeepSeek T=0 · 严格枚举            │
└──────────────────────────────────────────────────────────────────┘

外部数据源：financialdatasets.ai · yfinance · SEC EDGAR · ARK CSV CDN
            Alpaca · Tavily News API · DeepSeek LLM · OpenAI embeddings
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

机器人的自然语言层把任意中文 / 英文文本归类到 **19 个固定意图**之一，分类器是 DeepSeek temperature=0 + JSON 输出 + 白名单枚举校验。**任何不在白名单的输出都 fallback 到 `unknown`**，保证行为有界。

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
```

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

## ⑨⑩ Portfolio Risk Agent · 组合风险监控

第九和第十个 agent 是一对配合的组合风险 cron：⑨ 每个交易日盘后推送当日
风险快照（集中度 + 行业暴露 + P&L + 回撤 + 7 天财报风险），⑩ 周五额外
推送周度复盘。两者都接入 ⑥ 推送优先级系统：正常一天 P2 静默；单日亏损
≥ 5% 或多因子叠加自动升 P0；周报始终 P1（操作员可见）。

| Cron | 时间 | 行为 | Priority |
|---|---|---|---|
| ⑨ Portfolio Risk | Mon-Fri **18:30 ET** | 实时拉 RiskReport 全景 | 见 ladder |
| ⑩ Portfolio Weekly | Fri **19:00 ET** | 周复盘 + 月回报 + 1M 回撤 + 行业暴露 | 固定 P1（floor）|

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

**⑩ Clean Week P1 卡（floor 触发）：**

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
(per-position 周表现归因待开发 → Phase 2.5)
```

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

### Phase 2.5（推迟项目）

每持仓周表现归因（"本周最佳 CRM +6%、最差 IBM -7.5%"）需要每日 positions 快照表，Alpaca 不直接提供 per-position 历史曲线。当前 ⑩ 卡片显式标 `(per-position 归因待开发 → Phase 2.5)`，避免用"自买入以来"等误导性数字。

### 测试覆盖

- **20 个 portfolio smoke**（pipeline edges + sign convention + invested_value derivation）
- **5 个 priority floor**（关闭 Stage 2.5 dry-run 发现的 Issue 3）
- **19 个 priority ladder**（覆盖 8 个 spec ladder 案例 + multi-factor 叠加 + held-trumps-watchlist）
- **9 个 byte-equal pin**（4 个 formatter × 多 fixture）
- **13 个 cron integration**（priority threading / archive write / trace_json / responder_name 标签 / CronTrigger day_of_week pin / byte-equal cross-surface）

总计 28 个 Phase 2 新增测试。

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
| financialdatasets.ai | 基本面、价格、内部人交易、财报 | 主商业 API；schema 一致 |
| yfinance | 备用价格、期权链（OI + volume） | 免费；驱动"期权异动检测器" |
| SEC EDGAR（通过 `edgartools`） | 13F-HR 机构持仓 filing | 权威源 |
| ARK Invest CDN | ETF 每日持仓 CSV | 公开免费；日级别粒度 |
| Alpaca（paper） | 实时价格、账户持仓 + P&L | 免费；驱动 `/portfolio`、`/pnl`、streamer alerts |
| Tavily News API | 归因 + 验证用的新闻搜索 | 搜索引擎级别质量，无需爬虫 |
| DeepSeek (`deepseek-chat`) | 生成 + 验证 + 意图分类 | 中英双语下的性价比最高 |
| OpenAI `text-embedding-3-small` | RAG 记忆 embedding | 便宜准确，小维度 |

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
└── portfolio_weekly_to_telegram.py  # ⑩
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
- **Web 仪表盘** —— FastAPI + React + Tailwind，覆盖 `archive.db` 自动推送 feed、trace 回放、用户问答 mode
- **观测层 SDK** —— `v2/observability/` 对 FD / DeepSeek / Tavily / 意图分类器做 monkey-patch，所有 agent 自动产 trace
- **163 个 sandbox 单元测试** —— Phase 0/1/2 累计：21 个 earnings 卡片 byte-equal + 9 个 portfolio formatter byte-equal + 17 个 earnings priority + 19 个 portfolio priority + 5 个 priority floor + 10 个 portfolio pipeline edges + 13 个 portfolio cron integration + 10 个 earnings cron integration + 9 个 earnings pipeline + 9 个 archive migration + 14 个 intent classify + 18 个原始 priority + 13 个 observability + 余下分布

### 进行中 / TODO

- **Phase 2.5 (optional)** —— per-position 周表现归因（每日 positions_snapshot 表 + 早间快照 sub-cron）
- **Phase 3 · SEC 监控** —— 8-K / Form 4 / 10-Q 实时跟踪（扩展现有 `edgartools` 用法）
- **Phase 4 · Macro Agent** —— FOMC / CPI / PCE / NFP 通过 FRED + Tavily 推送
- **Phase 5 · Market Regime + ARK 显著调仓告警**
- **Phase 6 · News 重构 + 3-tier Universe**
- 持续：Migration system audit
- GitHub Actions CI（pytest 在 main 上跑）
- 多用户支持（每个 chat 独立 watchlist + 持仓）
- 接入现有 v2 event-study 框架做 backtest
- Alpaca 从 paper 切换到 live（paper 充分验证后改一个配置开关）

---

## 致谢

仓库外壳和原始教育版 `app/` 目录来自 [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)——一个开源 AI hedge fund 概念项目。`v2/` 目录——也就是本 README 描述的整个另类数据 agent 系统（六个盘后 agent + 两个财报 cron + 两个组合风险 cron、盘中 Streamer、含 19 个 NL intent 的 Telegram bot、5 层防幻觉机制、推送优先级系统、所有 SQLite + ChromaDB 存储、所有 systemd 部署脚手架）——是在那个基础上**完全从零搭建的独立项目**。

## License

MIT。教育用途。**不构成投资建议**。
