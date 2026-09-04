# `v2/agent` — 模型驱动的 Agent 循环（与现有单跳路由对比）

新增包，**不修改 `v2/bot` 任何一行**。它把同一套 24 个 responder 重新暴露成
模型可调用的工具，让模型自己决定调什么、调几次、按什么顺序调，从而与生产环境
现有的单跳意图路由形成可测量的对比。

### PyCharm / IDE：直接点绿三角

不想敲命令行就打开这两个文件，点运行按钮（顶部常量可改问题和模式）：

| 文件 | 需要什么 |
|---|---|
| `v2/agent/run_demo.py` | 什么都不需要，回放录制轨迹 |
| `v2/agent/run_compare.py` | 一个 LLM key（`.env` 里的 `DEEPSEEK_API_KEY` 即可） |
| `v2/agent/run_tests.py` | 什么都不需要，35 个测试（不走 pytest） |

这三个文件都会自己把仓库根目录补进 `sys.path`，所以不依赖任何 IDE 配置
（不用改 Working directory，也不用把根目录标成 Sources Root）。

跑测试用 `run_tests.py` 而不是直接运行 `test_agent.py`：PyCharm 见到 `test_` 开头的
文件名会自动切到 pytest runner，而 pytest 插件依赖 `setuptools`、项目的
`v2/conftest.py` 依赖 `python-dotenv`——干净环境里两样都没有，还没跑到测试就
collection error。想走 pytest 的话先 `pip install setuptools pytest python-dotenv`，
并把运行配置的 **Working directory 设为仓库根目录**。

### 命令行

全部命令都在**仓库根目录**执行。

```bash
# 零 API key，回放录制轨迹
python3 -m v2.agent.cli --demo

# 只花 LLM 的钱（工具层用录制观测，不需要 FD / Alpaca key）
python3 -m v2.agent.cli "我持仓里哪只最危险" --mode both --tools fixture

# 全真实（需要现有 .env 里的数据源 key）
python3 -m v2.agent.cli "我这周为什么亏钱" --mode agent --tools live

python3 -m v2.agent.test_agent               # 31 tests，无需 pytest
```

---

## 1. 差别在哪：控制流归谁

```python
# 现状 —— v2/bot/commands.py:669 cmd_nl，24 分支 if/elif
intent = classify(text)            # LLM 选一个 label
answer = DISPATCH[intent](args)    # 代码选函数，调用恰好一次，结束

# 本包 —— v2/agent/loop.py:run_agent
while not done:
    response = llm(messages, tools)          # 模型看着完整历史决定下一步
    if not response.tool_calls: break
    results  = execute(response.tool_calls)  # 并行执行
    messages += observations(results)        # 结果回灌，影响下一步
```

**LLM 在循环外 vs LLM 在循环里。** 其余全部差异都是这一条的推论：

| | 单跳路由（现状） | Agent 循环（本包） |
|---|---|---|
| 执行路径 | 编译期确定，共 24 条 | 运行时长出来，步数不定 |
| 反馈闭环 | 无——分类器看不到工具返回 | 有——每个观测都进入下一次决策 |
| 工具失败 | 异常冒泡到 `main._error_handler`，用户看到异常类名 | 变成 observation，模型换路线继续 |
| 并行 | 不存在（一次一个） | 一轮内扇出，`ThreadPoolExecutor` |
| 谁写答案 | 没人——原样返回工具卡片 | 模型写，但受 grounding 检查约束 |
| 能回答的问题 | 24 个 responder 的**并集** | 这些工具的**组合空间** |
| 怎么验证 | `assert output == expected`（确定性） | 轨迹指标 + 分布（非确定性） |

### 一个具体例子：「我持仓里哪只最危险」

**基线**：`classify → risk_view → 返回组合级风险卡`。这张卡有集中度和行业暴露，
但没有财报日期、没有内部人交易——**它无法按"综合风险"给个股排序**，因为需要的
事实分散在别的工具里。

**Agent**（`--demo` 的真实轨迹）：

```
步骤 1  portfolio_view()                             ← 先知道持有什么
步骤 2  并行 7 个：risk_view / earnings_view×2 /      ← 用步骤 1 的结果决定查谁
        insider_view / eight_k_view×2 / explain_move
        └ eight_k_view(SMCI) 超时失败 ✗
步骤 3  explain_move(SMCI)                           ← 绕过失败的工具补数据
步骤 4  初稿含「隐含波动率 78.4%」→ grounding 拦截      ← 该数字无任何工具来源
步骤 5  重写，去掉无来源数字 → 16/16 数字可溯源
```

结论「CRWD 最危险」不存在于任何单个 responder 中，是组合出来的。

---

## 2. 五个机制，各自对应一种真实故障

无边界的 `while` + 会犯错的模型 = 不可用。`loop.py` 里的每个机制都对应一个这个
循环实际会产生的故障：

| 机制 | 位置 | 防的是什么 |
|---|---|---|
| 预算（步数/工具数/墙钟）+ 强制收尾轮 | `loop.py` | 跑飞、以及"超时后什么都没有" |
| 错误即观测 | `registry.py:ToolRegistry.call` | 单个数据源抖动毁掉整轮 |
| 重复调用抑制 | `loop.py:_execute_calls` | tool-calling 循环最典型的死循环 |
| 近因加权上下文压缩 | `context.py` | 这些工具返回的是人读的卡片（`risk_view` >2KB），4 轮就撑爆窗口 |
| 数字溯源检查 + 一次重写 | `grounding.py` | 让模型写字之后重新拿回原来的防幻觉保证 |

### 关于 grounding：原来的招牌没有丢

`intent.py` 的原始设计信条是 *"The LLM never decides what to say, only which tool
to call"*。允许模型撰写答案就放弃了这条，所以必须换成一条**可机械执行**的规则：

> 答案里出现的每一个数字，都必须能在本轮某个工具返回里找到。

无需第二个 LLM、无额外成本。它抓不到"用正确数字讲错故事"，也不声称能抓——它抓的
是**凭空捏造的数字**，这是研报形态输出里唯一致命的失败模式。序数、计数、年份豁免，
模型自己算出来的数要求说明输入。检查不通过 → 带着未溯源清单退回重写一次。

对比时必须诚实的一点：**基线的溯源率恒为 100%**，因为它压根不写字。这是它真实的
优势，代价是它只能回答单工具能回答的问题。CLI 的对比表里明写了这句。

---

## 3. 权限边界

24 个工具里有 4 个会写 `state.db`（watchlist 增删、alert 增删）。能自主写入的
循环和只读的循环是两个风险等级，所以写操作**默认关闭**，需要 `--allow-mutations`
显式打开；被拦时模型收到的是一条可读的 observation（"说明你本来要改什么，不要重试"），
而不是静默失败。

---

## 4. 为什么工具层是惰性的、为什么不用 LangChain

- `registry.py` 里存的是**点分路径字符串**而不是函数对象，工具真的被调用时才
  `importlib` 解析。所以本包在裸沙箱（没有 numpy / telegram / langchain）里也能
  导入和测试，同时 bot 的启动开销不变。
- LLM 客户端是 stdlib `urllib` 直接对 OpenAI 兼容的 `/chat/completions` 协议，
  没有引入新依赖。Agent 循环的工程难点正是消息列表、tool_call 载荷和 token 记账，
  这三样不应该藏在框架后面。副作用是 provider 可移植：改 `AGENT_LLM_BASE_URL`
  即可切 DeepSeek / Groq / Gemini / 本地 Ollama。

```bash
AGENT_LLM_BASE_URL=https://api.deepseek.com/v1   AGENT_LLM_MODEL=deepseek-chat
AGENT_LLM_BASE_URL=https://api.groq.com/openai/v1 AGENT_LLM_MODEL=llama-3.3-70b-versatile
AGENT_LLM_BASE_URL=http://localhost:11434/v1      AGENT_LLM_MODEL=qwen3   # 零成本
```

---

## 5. 测试策略：为什么不能沿用现有那套

现有项目有 490 个 sandbox 测试，能断言"格式化卡片字节相等"——**这件事做得到，
恰恰因为它不是 agent**。同一个输入两次可能走不同路径、调不同工具、步数都不同，
`assert output == expected` 直接失效。

所以这里测的是**控制流**，模型被脚本化（`ScriptedLLM`），把"工具失败能不能恢复"
从轶事变成断言：

```
test_multi_step_run_uses_earlier_results_to_choose_later_calls
test_failed_tool_does_not_end_the_run
test_repeated_identical_call_is_suppressed
test_budget_exhaustion_still_produces_an_answer
test_ungrounded_answer_triggers_one_repair_round / test_repair_happens_at_most_once
test_old_observations_are_compressed_but_recent_ones_are_not
test_baseline_cannot_answer_a_multi_hop_question
```

`fixtures.py` 提供录制观测，让评测确定性可复现——**工具走网络的 agent 根本无法做
回归测试**。这也顺带解决了"别人 clone 下来没有付费数据源 key"的问题。

---

## 6. 基线的分类器：优先用生产那份，缺依赖时用同源移植

`v2/bot/intent.py` 依赖 LangChain。如果运行环境只装了一个 LLM key（比如新建的
conda env），基线那半边会 `ModuleNotFoundError`，对比只剩一半。

所以 `baseline.resolve_classifier()` 优先 import 生产分类器；import 失败时退回
`intent_port.py`——它**不复制 prompt**，而是用 `ast.literal_eval` 从
`v2/bot/intent.py` 源码里把 `_SYSTEM_PROMPT` 和三个白名单读出来。因此 bot 改了
prompt，基线跟着改；常量被重命名则显式报错，而不是默默测一份过期副本。CLI 会打印
本次用的是哪一个。

## 7. 目前还没有的（下一步）

- **评测集**：80–120 条标注了期望工具序列和期望事实的 query，指标为工具选择准确率、
  多跳完成率、溯源率、步数/token/延迟。有了它，`--mode both` 的单例对比才能变成
  一张有说服力的消融表（单跳 vs ReAct vs ReAct+反思）。
- **真实录制**：`fixtures.py` 现在是手写的合成数据，形状与真实 responder 一致；
  换成从真实运行捕获的 cassette 是 drop-in 替换。
- **接回 bot**：目前只有 CLI 入口，没有接到 Telegram。生产路径保持单跳不变是刻意的
  ——在有确定性要求的推送场景，单跳仍然是更合适的选择。
