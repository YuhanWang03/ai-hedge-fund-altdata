# Agent V2 评测

两套东西，目的不同：

* **`run_eval`（种子集）**：19 条路由用例加 12 条回答纪律用例，零网络、零模型，
  秒级完成。它证明"路由对、校验规则生效"，是提交前的回归门槛。
* **`run_benchmark`（V1 对比集）**：V1 的 89 条开发集加 15 条留出集原样移植，
  V1 的事实答案键、行为断言和禁止归属全部保留，只把 V1 工具名翻译成 V2 能力。
  它回答"V2 比 V1 好在哪、差在哪、贵多少"。

## 三档模式，一套答案键

| 模式 | 是什么 | 需要 |
|---|---|---|
| `v1_baseline` | V1 线上单跳：标注意图，一次 responder 调用 | 无 |
| `v2_rules` | V2 规则规划器加确定性合成器 | 无 |
| `v2_llm` | V2 线上配置：LLM 规划器加 LLM 合成器，校验器加一轮 repair，再不通过回退确定性摘要 | 模型 key |

`v1_baseline` 用的是标注好的意图（相当于先知分类器），`v2_rules` 要从原文自己路由，
两列难度不同。`v2_rules` 的事实召回是下限：确定性合成器只回显证据，不做计算。

## 两层 fixture

| `--fixtures` | 研究 / 市场能力从哪来 | 事实答案键 |
|---|---|---|
| `v1`（默认） | V1 录制的卡片，`research.stock` 由该 ticker 的卡片按 focus 拼装 | 门槛 |
| `engine` | 引擎结构的 envelope：`eval/recorded/` 里有真实录制就回放，否则用 `engine_fixtures` 离线合成 | 仅参考（星号标注） |

`engine` 层的通过判定是工具召回加校验通过加无错误。账户、状态、宏观、ETF、13F
在两层里都用 V1 卡片，因为它们包装的是同一批 responder。

离线合成走的是研究引擎的 intelligence 层（纯函数）加真实的 V2 适配器，所以证据 id、
派生指标、限制条目都是生产代码生成的；市场 envelope 走真实市场适配器加确定性价格序列。
`v2/data` 是生产专用包，不在仓库里，`_data_shim` 只在它缺席时装占位符。

真实录制需要在有 `v2/data` 和 provider key 的机器上跑：

```
python -m v2.agent_v2.record_fixtures --live                 # 全部 benchmark ticker × focus
python -m v2.agent_v2.record_fixtures --live --tickers NVDA,AMD --focuses overview,earnings
```

部分录制也有用：有录制的 key 回放，其余合成。

## 模型在环

```
export AGENT_LLM_API_KEY=...            # 也可用 DEEPSEEK_API_KEY / OPENAI_API_KEY
export AGENT_LLM_BASE_URL=...           # OpenAI 兼容端点
export AGENT_LLM_MODEL=...

python -m v2.agent_v2.run_benchmark --modes v1_baseline,v2_rules,v2_llm --repeat 3 --fixtures engine --markdown report.md
python -m v2.agent_v2.run_benchmark --modes v2_rules,v2_llm --repeat 3 --holdout --fixtures engine --markdown report.md
python -m v2.agent_v2.run_benchmark --modes v2_rules,v2_llm --repeat 3 --fixtures v1 --markdown report.md
```

三次重复是必须的：V1 的经验是同配置两轮的失败清单只重合三分之一。报表里
"稳定失败"是三次都失败的用例，只有它们值得修；"不稳定用例"是噪声。

`v2_llm` 特有的列：

* **校验结果** `clean / repaired / fallback / knowledge`：一次通过、repair 后通过、
  两次都不通过而回退到确定性摘要、通用知识回答。
* **校验器疑似误报**：答案键全部通过但校验器仍拒绝的用例。V1 第 14 轮的教训是
  校验器的错误率要在它拒掉的那份文本上量，否则读数永远是 0。只在 `v1` fixture 下有意义。
* **重写丢事实**：初稿满足答案键、repair 后反而丢了。
* **每通过 token**：成本对比的主指标。

## 没有 key 时：harness 演练

```
python -m v2.agent_v2.run_benchmark --modes v2_llm --simulate 0.25 --repeat 3
```

`--simulate` 用 `eval/simulated_llm.py` 里的模拟模型：规划提示回规则规划器的计划 JSON，
合成提示引用证据写句子，按 `NOISE` 的概率塞一个证据里没有的数字让校验器拒绝，
repair 时去掉它，再按一半概率"顽固"重犯以触发回退。它验证的是评测框架的每条路径
和每一列数字的计算方式，**不是任何模型的分数**。

## 留出集纪律

留出集只看不调。开发集和留出集的差距是手写规则过拟合的直接读数：
规划器补完开发集缺口后是 96% 对 60%（V1 卡片）。留出集剩下的失败应该交给
`v2_llm` 去量，而不是继续加正则。

## 文件

| 文件 | 作用 |
|---|---|
| `cases.py` `answer_cases.py` `fixtures.py` `runner.py` `scoring.py` | 种子集 |
| `benchmark_cases.py` | V1 案例移植与工具名翻译 |
| `benchmark_fixtures.py` | V1 卡片到 V2 能力的注册表，含 `engine` 层 |
| `engine_fixtures.py` | 离线合成引擎结构 envelope |
| `recorded.py` | envelope JSON 录制与回放 |
| `simulated_llm.py` | 模拟模型 |
| `benchmark.py` | 三档模式、评分、稳定性、校验器轴、报表 |
| `test_benchmark.py` | 契约测试 |

## 在 VPS 上一键跑

`vps_run.sh` 在生产机上用独立的 git worktree 跑整套模型在环评测，不碰生产工作树和
任何 systemd 服务；复用生产 poetry 环境、`.env` 里的 key 和 git 忽略的 `v2/data` 包。

```
ssh root@<vps>
cd /root/hedge-fund && git fetch origin claude/agent-v2-design-review-jozhco
nohup bash <(git show origin/claude/agent-v2-design-review-jozhco:v2/agent_v2/eval/vps_run.sh) --push \
    > /root/hedge-fund/logs/agent_v2_eval.nohup 2>&1 &
tail -f /root/hedge-fund/logs/agent_v2_eval.nohup
```

顺序：单测 → 真实录制引擎 envelope（FD key 缺失时容错，缺的合成）→ 四组 benchmark
（engine 与 v1 两层 fixture，各跑开发集与留出集，`v2_llm` 三次重复）→ 报告写到
`logs/agent_v2_eval/<时间戳>/report.md`，`--push` 会把它提交到分支的
`v2/agent_v2/eval/reports/`。`--push-recorded` 连录制的 envelope 一起提交（可能有几 MB）。

预算：DeepSeek 上约 800 到 1200 次模型调用，`--workers 3` 下大约 30 到 60 分钟。
`EXTRA="--simulate 0.2"` 可以在 VPS 上先做一次不花钱的演练。
