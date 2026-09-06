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
  以及部分趋势判断把"最新在前"的列表当作"最旧在前"（Munger 的毛利率趋势）。
  这些按原样保留，因为改了就不是同一套规则；要修应当作为独立的调整并记录。

## 数据客户端

`build_snapshot()` 接受任何满足 `PersonaDataClient` 协议的对象。生产环境的
`FDClient`（`v2/data/`，不在仓库里）可以直接传入：`adapt_client()` 会兼容它的
位置参数写法，缺的方法（如 `search_line_items`）在设了
`FINANCIAL_DATASETS_API_KEY` 时由内置 HTTP 客户端补上。

## 未做的事

- 没有 Web 接口和实验室页面的接入，这一层只是引擎。
- 没有历史回测；LLM 层做回测有前视偏差，确定性打分层可以包成
  `v2/backtesting.Strategy`，尚未实现。
- 没有落库；`PersonaSignal.to_dict()` / `CommitteeResult.to_dict()` 已是 JSON，
  存哪里由调用方决定。
