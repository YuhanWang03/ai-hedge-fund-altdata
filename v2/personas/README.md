# v2/personas · 模拟投资人

十三位规则驱动的"模拟投资人"，移植自 [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)
（tag `v2026.5.14`，MIT）。每位投资人是一份**确定性的打分清单**，不是聊天机器人：
输入一份基本面快照，输出多空信号、置信度、逐项得分和依据。LLM 不参与判断，
只在可选的"解读"层把数字翻译成一段有引用的文字，并经过归属检查。

```
python -m v2.personas --demo                     # 内置合成数据，无需网络
python -m v2.personas AAPL MSFT NVDA             # 需要 FINANCIAL_DATASETS_API_KEY
python -m v2.personas AAPL --as-of 2026-03-31 --personas warren_buffett,ben_graham --json
python -m v2.personas AAPL --narrate             # 追加 LLM 解读（用 v2/agent/llm.py 的配置）
python -m v2.personas --list
```

## 十三位投资人

| key | 投资人 | 风格 | 数据周期 | 额外输入 |
|---|---|---|---|---|
| `warren_buffett` | 沃伦·巴菲特 | 以合理价格买入卓越企业 | ttm×10 | — |
| `charlie_munger` | 查理·芒格 | 只买优秀企业，价格公道即可 | annual×10 | 内部人、新闻 |
| `ben_graham` | 本杰明·格雷厄姆 | 安全边际，捡便宜货 | annual×10 | — |
| `peter_lynch` | 彼得·林奇 | 在日常生意里找十倍股 | annual×5 | 内部人、新闻 |
| `phil_fisher` | 菲利普·费雪 | 深度"闲聊法"调研成长股 | annual×5 | 内部人、新闻 |
| `bill_ackman` | 比尔·阿克曼 | 大胆持仓，推动变革 | annual×5 | — |
| `cathie_wood` | 凯茜·伍德 | 相信创新与颠覆 | annual×5 | — |
| `michael_burry` | 迈克尔·伯里 | 深度价值，逆向 | ttm×5 | 内部人、新闻 |
| `mohnish_pabrai` | 莫尼什·帕伯莱 | 低风险翻倍 | annual×8 | — |
| `stanley_druckenmiller` | 斯坦利·德鲁肯米勒 | 不对称的成长机会 | annual×5 | 内部人、新闻、价格 |
| `aswath_damodaran` | 阿斯瓦斯·达摩达兰 | 故事、数字、严谨估值 | ttm×5 | — |
| `nassim_taleb` | 纳西姆·塔勒布 | 尾部风险、反脆弱、凸性 | ttm×10 | 内部人、新闻、价格 |
| `rakesh_jhunjhunwala` | 拉克什·金君瓦拉 | 印度"大牛" | ttm×10 | — |

## 模块

```
v2/personas/
├── models.py        Record / SubScore / Evaluation / PersonaSignal
├── data.py          PersonaDataClient 协议 · FinancialDatasetsClient(urllib) · adapt_client()
├── snapshot.py      PersonaSnapshot：每只股票取一次数据，13 人共用，content_hash 可做缓存键
├── base.py          Persona 基类：evaluate() → decide() → analyze()；共享的判决规则
├── <investor>.py    十三位投资人，各自一个文件，上游的打分函数逐条保留
├── registry.py      key → 类，懒加载
├── committee.py     run_committee()：并行取快照、逐人打分、按置信度加权投票、排名
├── store.py         SQLite（data/personas.db）：委员会运行记录、逐人信号（预留 fwd_1m/fwd_3m 前向收益列）、当日快照缓存
├── narrate.py       可选 LLM 解读：信号与置信度不可改，数字须可溯源
├── fixtures.py      合成快照（quality / distressed / empty），测试与 --demo 用
└── __main__.py      CLI
```

## 与上游的差异

- **判决由规则给出，不再由 LLM 给出。** 上游每位 agent 最后一步把 facts 交给 LLM 输出
  signal / confidence；这里用 `Persona.decide()` 编码同一份 prompt 里的信号规则
  （默认：得分率 ≥70% 看多、≤30% 看空，价值型投资人还要求安全边际为正），
  置信度是得分率的确定性函数。上游本身在打分层已经算好了这些数字，判决只是照念。
- **一次取数，十三人共用。** 上游每个 agent 各取各的，一只股票 40–60 次 API 调用；
  这里 `build_snapshot()` 最多 8 次，只取被选中的投资人真正需要的部分。
- **弃权是显式的。** 缺基本面就 `abstained=True`、confidence 0，不再默认 neutral/50，
  投票时不计入。
- **委员会是算术，不是 LLM。** 上游的 Portfolio Manager 把 13 份意见塞给模型综合；
  这里 `tally()` 按置信度加权求共识分（-1..+1），并给出多/空/中票数、一致度、
  平均置信度，分歧一目了然。
- **LLM 只做解读，且受约束。** `narrate()` 给模型的是已经定好的信号和数字；
  改了信号或置信度的回复直接丢弃，回复里的每个数字用 `v2/agent/grounding.check`
  核对，结果写在 `narrative_grounded`。

## 移植时发现的上游问题

每个模块的 docstring 记录了与上游的差异，主要是三类：

- **读不到的字段。** 上游 Damodaran 从 `FinancialMetrics` 读 `revenue`、`free_cash_flow`、`beta` 等
  该模型根本没有的字段，DCF 从未真正跑过；Munger 按 `transaction_type` 判断内部人买卖，
  而数据源不返回这个字段。移植版从 line items 补齐前者，用 `transaction_shares` 的正负补齐后者。
- **满分对不上。** Ackman 声明满分 20，规则最多给 16；Wood 声明 15，估值项最多 3 分；
  Graham 估值项可给 7 分却只声明 6。移植版保留上游的判决阈值，`max_score` 按规则真实上限报告。
- **周期语义。** 上游把 10 期 TTM 数据当作 9 年算 CAGR（Jhunjhunwala、Damodaran），
  增长率被压低到约四分之一，两位对几乎所有股票看空。**已修正**：`Persona.cagr()`
  按行的 report_period 实际跨度年化（缺日期时 TTM 按每期 0.25 年），两位的
  docstring 记录了这次调整。Munger 的毛利率趋势和 Jhunjhunwala 的两处增长一致性循环把"最新在前"当"最旧在前"比较，也已修正并记录。

## 数据客户端

`build_snapshot()` 接受任何满足 `PersonaDataClient` 协议的对象。生产环境的
`FDClient`（`v2/data/`，不在仓库里）可以直接传入：`adapt_client()` 会兼容它的
位置参数写法，缺的方法（如 `search_line_items`）在设了
`FINANCIAL_DATASETS_API_KEY` 时由内置 HTTP 客户端补上。

## 实验室接入

`web/backend/app/routers/committee.py` 把这一层挂到工作台的实验室页面（"投资人委员会"工具）：

| 接口 | 作用 |
|---|---|
| `POST /api/lab/committee` | 输入来源 `source`：`holdings`（Alpaca 多头持仓，带权重和增持/持有/减持标签）、`watchlist`、`tickers`（≤60 只）、`screening`（先跑股票筛选再评审）。可选 `personas`、`as_of`、`top_n`、`max_weight`。 |
| `GET /api/lab/committee/runs` · `/runs/{id}` | 落库的运行记录与完整结果 |
| `GET /api/lab/committee/personas` | 13 位投资人的元数据（前端的多选） |
| `GET /api/lab/committee/scoreboard` | 回填前向收益后的逐人命中率（目前为空） |

持仓标签是一条透明规则（`_holding_action`）：共识 ≥ +0.2 且一致度 ≥ 50% 为"增持候选"，
但权重已达 `max_weight` 则"持有"；共识 ≤ −0.2 为"减持候选"；其余"持有"。

## 前向收益与解读

- `forward.py`：`backfill_forward_returns()` 给已过 30 / 91 天的投票回填 `fwd_1m` / `fwd_3m`，
  只用投票日之后的价格，没有前视。调度器每天 02:30 ET 跑一次（⑯，不推送），
  也可 `python -m v2.personas.forward --scoreboard` 或 `POST /api/lab/committee/backfill` 手动触发。
  `GET /api/lab/committee/scoreboard` 返回逐人命中率。
- `POST /api/lab/committee/narrate`：对某次运行里某一格调用 `narrate()`，写回该运行的结果。
  页面上是详情卡里的「LLM 解读」按钮；改了信号或置信度的回复会被丢弃并报错，
  数字溯源结果显示为 ✓ / ⚠。

## 未做的事

- 历史回测：LLM 层做回测有前视偏差；确定性打分层可以包成 `v2/backtesting.Strategy`，尚未实现。
- 接口是同步的（沿用实验室 240 秒超时）。规则层有缓存时几秒即返；解读一次一格，
  也在超时内。批量解读应照 `routers/research.py` 的异步任务模式。
