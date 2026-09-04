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
| `v2/agent/run_tests.py` | 什么都不需要，125 个测试（不走 pytest） |
| `v2/agent/run_router.py` | 什么都不需要，路由层打分 |
| `v2/agent/run_anomaly_assist.py` | 什么都不需要，B1 异动补齐打分 |
| `v2/agent/run_mda_reader.py` | 什么都不需要，B2 MD&A 解读打分 |
| `v2/agent/run_eval.py` | baseline 档零 key；跑 agent 需 LLM key |

这三个文件都会自己把仓库根目录补进 `sys.path`，所以不依赖任何 IDE 配置
（不用改 Working directory，也不用把根目录标成 Sources Root）。

跑测试用 `run_tests.py` 而不是直接运行 `test_agent.py`：PyCharm 见到 `test_` 开头的
文件名会自动切到 pytest runner，而 pytest 插件依赖 `setuptools`、项目的
`v2/conftest.py` 依赖 `python-dotenv`——干净环境里两样都没有，还没跑到测试就
collection error。想走 pytest 的话先 `pip install setuptools pytest python-dotenv`，
并把运行配置的 **Working directory 设为仓库根目录**。

### 命令行

> **配置运行请用环境变量或命令行参数，不要改这些文件里的常量。**
> 它们被 git 跟踪，本地一改，下次 `git pull` 会以
> `local changes would be overwritten by merge` 中止 —— 而这个中止很容易被忽略，
> 后果是你以为在跑新代码、其实跑的是旧的。
>
> ```bash
> EVAL_REPEAT=3 EVAL_WORKERS=8 python3 -m v2.agent.run_eval
> AGENT_QUERY="我这周为什么亏钱" python3 -m v2.agent.run_compare
> ```
> PyCharm：Run → Edit Configurations → Environment variables，
> 填 `EVAL_REPEAT=3;EVAL_WORKERS=8`。

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

## 7. 路由层：什么时候才值得花 10 倍成本

实测（CLI 对比表）：单跳 1 次 LLM 调用 / ~1.2s；agent 4–5 次 / ~11s / ~14k token。
**10 倍成本只在单跳答不了的问题上才划算**，所以需要一个路由层决定走哪条。

`router.py` 的硬约束：**路由本身不调 LLM**。用「分类器已经产出的 intent」+ 正则，
边际成本为零——否则为了省钱先花一次钱，逻辑不成立。

三种 mode 就是灰度顺序：

```bash
V2_AGENT_ROUTING=off           # 默认。全走现状，合并代码不改变任何线上行为
V2_AGENT_ROUTING=unknown_only  # 只接分类器的死胡同（现在回「❓ 没听懂」那些）
V2_AGENT_ROUTING=heuristic     # 全部信号
```

信号表（命中即进 agent，按置信度排序，先命中者胜）：

| 信号 | 判据 | 例子 |
|---|---|---|
| `superlative` | 最(排除最近/最新/最后) · 哪只 · 对比 · 谁更 | 我持仓里哪只最危险 |
| `collection` | 持仓里/关注的/watchlist + 无单一 ticker | 我持仓里有哪些快发财报了 |
| `multi_ticker` | ≥2 个非缩写的大写代码 | NVDA 和 AMD 对比一下 |
| `causal_portfolio` | 为什么 + 盈亏/组合词 + 无 ticker | 我这周为什么亏钱 |
| `compound` | 并且/还有/另外 或 ≥2 个问号 | AAPL 财报怎么样，另外内部人在卖吗 |
| `unknown_intent` | 分类器归类失败 | 今天有什么值得注意的 |
| `explicit_ask` | `/ask <问题>` | 用户强制，任何 mode 下生效 |

两个容易写错、已用测试钉死的细节：

- **`最近`/`最新`/`最后` 不是比较级**。「AMD 最近有什么 8-K」必须走快路径。
- **枚举类 intent 豁免**。「我关注了哪些股票」提到集合，但 `watchlist_view`
  一跳就返回整个集合；除非同时要求排序（「watchlist 里哪只最值得关注」），
  那时比较级信号优先。

### 打分

```bash
python3 -m v2.agent.run_router     # 零 key
```

36 条标注样例（`samples.py`），三种 mode 全部 100%，指代消解 5/5。

> **这个 100% 不值得高兴**：规则和样例都是同一个人写的，它测的是内部自洽性，
> 不是真实准确率。它的作用是**回归护栏**——改信号表时立刻知道碰坏了什么。
> 真实准确率要等真实用户 query 进来标注之后才有意义。`samples.py` 是那个集合的种子。

## 8. 会话状态：让快路径能答更多问题

`session.py` 保留每个 chat 最近 6 轮（TTL 30 分钟，内存，重启丢失可接受）。

关键在于**指代消解发生在分类之前**：「那它财报呢」补全成「NVDA 财报呢」之后，
**单跳路径就能答**，根本不用进 agent。会话记忆在这里不是 agent 功能，
它是给 agent 减负的。改写对用户可见（回复顶部显示补全前后）。

## 9. 接到 bot：两行

`bot_bridge.py` 是 bot 唯一需要知道的文件。改动点是
`v2/bot/commands.py:934` 那个 `else: # "unknown"` 死胡同：

```python
else:  # "unknown"
    if bot_bridge.enabled():
        reply = await bot_bridge.handle_nl(text, chat_id, parsed=parsed,
                                           on_progress=progress)
        await placeholder.edit_text(reply.answer, parse_mode="HTML")
    else:
        ...现有的「❓ 没听懂」...
```

`enabled()` 默认 `False`，**合并这段代码不改变任何线上行为**，之后靠环境变量灰度。

两个设计细节：
- `parsed` 由调用方传入。bot 走到这个分支时已经付过一次分类的钱，桥接层再分类一次
  就是每条消息双倍成本。
- 进度回调用 `make_threadsafe_progress()` 跨线程边界（`run_agent` 是同步的、跑在
  executor 线程，`edit_text` 是 bot 事件循环的协程），fire-and-forget：
  掉一条进度绝不能把答案带下去。

## 10. B1：异动归因的低置信补齐（`anomaly_assist.py`）

`attributor.py` 的流程止于 Verifier：如果每条理由都被打成 低，或者实体过滤把新闻
全滤光了，卡片就这么推出去——读者得到的是「涨了但不知道为什么」。新闻搜索只有
一次机会，管线没有换个问法再试的能力。

B1 给它**恰好一次**补救机会。约束全部来自「这是无人值守的 cron」这一个事实：

| 约束 | 值 | 防的是什么 |
|---|---|---|
| 只在失败分支触发 | 已有 高/中 理由的异动完全不进这段代码 | 正常路径零成本 |
| 只读工具，4 个 | 8-K / Form 4 / 财报 / 资金流 | 新闻搜不到时，原因通常藏在申报里 |
| 每轮上限 | 默认 3 条，按涨跌幅+量比+信号数+逆势排序 | 某天 20 只票异动不能变成 20 次 agent 运行 |
| 硬截止 | daemon 线程 + 超时放弃 worker | 卡住的 HTTP 调用绝不能让 cron 无法退出 |
| 结构化输出 | JSON + 高/中/低 白名单校验 | 无人值守的输出必须可解析、可校验，不能是散文 |
| 溯源失败即丢弃 | 不重写、不协商 | 没人盯着的时候，沉默优于自信的猜测 |

**任何一步失败都返回 `None`，调用方原样推送既有卡片。** 确定性路径永远只被追加，
不被替换。

### 打分

```bash
python3 -m v2.agent.run_anomaly_assist    # 零 key
```

6 条样例异动，三个被选中的刻意覆盖三种结局：

```
  ARM   ✅ ok           8-K 里有 $2.40B 合同，量级能解释 +7.42% —— 补齐成功
  SMCI  🟡 no_finding   8-K 源超时，改查财报后如实报告「找不到」
  PLTR  ❌ ungrounded   产出「机构持仓下降 31.5%」，无任何工具来源 —— 整条丢弃

  无有效归因占比   4/6 = 67%   →   3/6 = 50%
  代价             3 次 agent 运行 · 8645 token
  丢弃             1 条未溯源，1 条如实报告找不到
```

被丢弃和「找不到」的条目原样推送现有卡片。**3 条里只有 1 条成功——这个数字不好看，
但它是诚实的**：另外两条本来就该失败，而系统正确地让它们失败了。

### 接到 cron

`scripts/anomaly_to_telegram.py` 是「归因 + 推送」单趟循环。不需要改成两趟：
资格要归因后才知道，但**排序键在归因前就有**，所以先排序、再按顺序花预算，
选出的集合与事后排序完全等价（有测试钉住这个等价性）。

```python
assistant = BudgetedAssistant()               # 读 V2_AGENT_ANOMALY_ASSIST，默认关
for anomaly in assistant.order(anomalies):    # 改：预排序
    attribute(anomaly, fd_client=fd, memory=memory)
    assistant.maybe_assist(anomaly)           # 新增：未启用时是 no-op
    ...现有的 chart / caption / push 一行不动...
print(assistant.summary())                    # 新增：本轮花了多少、补上几条
```

```bash
V2_AGENT_ANOMALY_ASSIST=true    # 默认关闭
```

## 11. B2：10-Q MD&A 措辞解读（`mda_reader.py`）

`v2/sec/ten_q_parser.py` 已经回答了**变了什么**：哪些 MD&A 段落相对上季是新增的、
新增了几条风险因素、有没有 going concern / 重大缺陷。这些是 diff 和正则的确定性事实，
自己就能升 P0，**这一层不能改动也不能抑制它们中的任何一条**。

管线答不了的是**这个变化意味着什么**。一段关于「客户验收周期拉长」的新增措辞，
读者要么认得要么划过去。这个判断是开放的，所以模型该在这儿——而且只做加法。

### 为什么这一层没有工具

本包前面几块是工具循环，因为它们的问题只能靠取数回答。这个问题不用：文本已经在
手里了，业绩数字也随卡片一起进来。为了「让 B2 看起来也像 agent」而接几个工具，
只会增加延迟、token 和失败面，换不到任何东西。**它就是一次受约束的调用**，
工程含量在输出之后发生的事情上。

### 校验比本包其他任何地方都严

别处的判据是「数字能溯源」。这里模型读的是散文，风险不在数字而在**转述**——
把一段披露悄悄说得比原文更好或更坏，才是财报推送里真正致命的失败。

所以每条解读必须附一段**逐字引用**，按空白归一化后做子串匹配，且
**只对照模型实际看到的那份语料**（段落发送前会截断，校验就对着截断后的文本做）——
否则引用可能"验证通过"于模型从未见过、只能靠猜的正文。引用找不到就丢弃，
不重写不软化。方向是闭合枚举，解读里的数字照样过数值溯源。

### 打分

```bash
python3 -m v2.agent.run_mda_reader     # 零 key
```

```
  ✅ CRWD   ok                [确定性部分：新增段落 3 · 新增风险因素 2]
       🔻 「extended customer acceptance cycles」
            → 企业客户签约到确认收入的周期在拉长，对后续收入确认节奏是压力
       🔻 「a charge of $18.4 million related to the restructuring of」
            → EMEA 销售组织重组已计提 $18.4M，后续还有最多 $6.0M
  🟡 AAPL   no_finding        新增段落全是会计政策模板，如实返回空
  ❌ BADCO  unquoted          引用不在原文中：management expects liquidity to normaliz
  ⚪ MSFT   nothing_to_read   本季无新增段落，一次调用都没发生（0 token）

  产出解读 2 条 · 拒绝 1 条 · 零成本跳过 1 个 · 代价 4140 token
```

BADCO 那条是这层存在的理由：一家 going concern 的公司，模型编了一句
「管理层预计年底流动性恢复正常」并标成利好。引用逐字查不到，整条被拒。

### 接到 cron

`v2/earnings/pipeline.py:run_summaries` 已经把 `ten_q_delta` 挂在 `EarningsSummary`
上了，所以接入发生在渲染侧，管线一行不动：

```python
reading = mda_reader.read_if_enabled(summary.ten_q_delta)   # 未启用时返回 None
card = format_earnings_summary(summary)                     # 现状，一行不改
if reading and reading.ok:
    card += "\n\n" + reading.render()                       # 纯追加
```

```bash
V2_AGENT_EARNINGS_READ=true    # 默认关闭
```

## 12. 评测集（`v2/agent/eval/`）

83 条标注 query，9 个类别。每条说清三件事：答案**需要**哪些工具、哪些事实必须活到
回复里、允许花多少。这个三元组把「答案看起来不错」变成一个数字。

> **第一版标注错了 17 条。** 首轮真实运行后用一条机械规则复核：
> **baseline 能通过的 case，按定义就是单跳能答的**。据此把 13 条从 agent 改回
> single_hop，另外 3 条是断言太弱被单跳蒙混过关（例如「NVDA 和 AMD 谁的财报更好」
> 只断言了 NVDA 的数字），补强后才真正需要多跳。还有 6 条**既没断言事实也没禁止
> 内容，必然空过** —— 这类 case 会对所有模式等量送分，是最难察觉的评测 bug，
> 现在有守卫测试拦着。

```bash
python3 -m v2.agent.run_eval --modes baseline          # 零 key
python3 -m v2.agent.run_eval                           # baseline / routed / agent
python3 -m v2.agent.run_eval --modes baseline routed agent agent_no_repair \
                             --workers 8 --out eval.json
```

**基线那一档完全不需要 API key**——它用样例标注的 intent 直接分发，工具层是录制观测。
所以任何人 clone 下来立刻能看到「现有系统得几分」，这就是对比的起点：

```
  mode          通过    通过率   工具召回  事实召回  溯源率  越界  超预算  工具/例
  baseline     42/83     51%     70%      64%    100%   0%    0%     0.8

  single_lookup  18/18 (100%)      multi_hop    3/14 (21%)
  cost_trap       6/6 (100%)       compound      0/8  (0%)
  causal          6/8  (75%)       recovery      0/5  (0%)
  honesty         6/7  (86%)       dead_end      0/7  (0%)
  ranking         3/10 (30%)
```

单跳在它设计要解决的问题上是满分，在需要跨工具组合的地方掉到 0–20%。
**这正是它的设计，不是它的缺陷**——数字只是把边界画出来了。

### 四个正交判据

一个 pass/fail 对改进几乎没用：工具调错和事实丢失长得一模一样，但要反着修。
所以每条按四个轴独立打分，`passed` 是它们的合取：

| 判据 | 说明 |
|---|---|
| 工具召回 | 需要的工具调到了吗。**多调不扣分**——多调是成本问题，计入成本指标 |
| 事实召回 | 该出现的事实活到回复里了吗。每个事实是一组可接受表述（`2026-09-06` / `9月6日` 都算） |
| 纪律 | 有没有调不该调的工具、有没有输出禁止内容（典型：这只没数据，就把另一只的数字搬过来） |
| 溯源 | 数字能否追到观测。**编数字的答案不是正确答案**，所以它在 `passed` 里而不是脚注里 |

### 消融

```
baseline           单跳（要打败的对象）
agent              完整循环
routed             路由逐条决定（生产形态）
agent_no_parallel  关并行 → 分离延迟收益
agent_no_repair    关溯源重写 → 分离该机制的价值
agent_tight        3 步 / 4 次工具 → 预算到底重不重要
```

`routed` 是最值得看的一行：它应该在通过率上贴近 `agent`，成本上贴近 `baseline`。
**如果没有，就说明路由的信号表是错的。**

### 溯源失败要能拆开看

首轮全量运行里，agent 的 17 条失败有 14 条是「数字无法溯源」。这个统计本身不可行动：
**模型编了一个数**和**模型把观测里两个数加起来但没写算式**在这个指标里长得一模一样，
而它们要反着修（前者收紧 prompt，后者考虑放宽校验）。

所以 `grounding.diagnose()` 把每个被拒的数字分类：`rounding`（观测里有近似值）、
`sum` / `difference`（存在算术解释）、`unknown`（找不到任何来源）。

比率（a/b×100）试过但**删掉了**：观测里数字一多，几乎任何目标都能被某个比率巧合命中，
那会把编造的统计洗白成「合法运算」，正好背离这个诊断的目的。同理，`sum` 只说明
「存在一个算术解释」，不等于模型真做了该运算 —— **只有 `unknown` 可以放心当作编造处理**。

### 评测集自己也要被测

评测代码是判别别人的代码，它的 bug 比被测代码的 bug 更坏——会产出一个看起来
权威的错数字。所以 `test_eval.py` 里除了打分器和执行器，还机械校验**答案键本身**：

> 每条 case 断言的事实，必须真的能在录制观测里找到。

否则那条 case 从构造上就不可能通过，套件会对所有模式**等量地**低报——这是最难
察觉的一类评测 bug。这条检查在写 case 的过程中就抓出过我自己标注的错误。

## 13. 目前还没有的（下一步）

- **评测集**：80–120 条标注了期望工具序列和期望事实的 query，指标为工具选择准确率、
  多跳完成率、溯源率、步数/token/延迟。有了它，`--mode both` 的单例对比才能变成
  一张有说服力的消融表（单跳 vs ReAct vs ReAct+反思）。
- **真实录制**：`fixtures.py` 现在是手写的合成数据，形状与真实 responder 一致；
  换成从真实运行捕获的 cassette 是 drop-in 替换。
- **接回 bot**：目前只有 CLI 入口，没有接到 Telegram。生产路径保持单跳不变是刻意的
  ——在有确定性要求的推送场景，单跳仍然是更合适的选择。
