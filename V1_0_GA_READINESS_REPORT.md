# Research Engine V1.0 GA Readiness Report

生成时间：2026-09-06  
候选版本：`research-v1.0-rc1`  
结论：**暂不升级到 `research-v1.0`，继续保持 RC。**

## 1. 总体结论

Phase 3D.1 的代码加固、生产部署和 Fresh Run 验证已经完成。13 只回归股票均成功完成研究，行业识别、事实约束、情景输出和 Provider 健康检查均通过；但严格 GA Coverage Gate 未全部通过，因此不满足版本冻结条件。

未通过 GA Coverage Gate 的股票：

- XOM：4/9，`INDUSTRY_COVERAGE_LIMITED`
- GM：3/8，`INDUSTRY_COVERAGE_LIMITED`
- F：3/8，`INDUSTRY_COVERAGE_LIMITED`

这些股票保持 `PARTIAL_DATA`，系统已降低其研究置信度或显示行业覆盖不足，不使用推测值补齐。

## 2. 覆盖率 Before / After

| 股票 | 行业 | 加固前 | 加固后 | GA Gate | 说明 |
|---|---|---:|---:|---|---|
| AAPL | TECHNOLOGY | 4/4 | 4/4 | PASSED | 稳定 |
| NVDA | TECHNOLOGY | 4/4 | 4/4 | PASSED | 稳定 |
| MSFT | TECHNOLOGY | 4/4 | 4/4 | PASSED | 稳定 |
| JPM | FINANCIALS | 4/9 | 6/9 | PASSED | 新增贷款、存款等银行指标 |
| BAC | FINANCIALS | 1/9 | 6/9 | PASSED | SEC 全文与表格语义抽取显著改善 |
| XOM | ENERGY | 0/9 | 4/9 | LIMITED | 错误匹配已清除，但可靠油气分项/实现价格仍不足 |
| CVX | ENERGY | 6/9 | 7/9 | PASSED | 公司级产量、实现价格、上下游利润可追溯 |
| TSLA | AUTOMOTIVE | 2/10 | 7/10 | PASSED | 汽车与能源业务表格指标已结构化 |
| GM | AUTOMOTIVE | 2/8 | 3/8 | LIMITED | 交付/库存/现金流可得，汽车收入与毛利率仍缺可靠统一口径 |
| F | AUTOMOTIVE | 2/8 | 3/8 | LIMITED | 分部表较完整，但缺可靠公司级汽车收入/毛利率汇总 |
| WMT | GENERAL | 4/4 | 4/4 | PASSED | Provider 降级未复现 |
| KO | GENERAL | 4/4 | 4/4 | PASSED | 稳定 |
| CAT | GENERAL | 4/4 | 4/4 | PASSED | 稳定 |

说明：文档中的 ticker 覆盖目标仅用于回归验证，没有写入 ticker-specific 数值或特殊硬编码。

## 3. 三个重点样本

### JPM

- 覆盖率：4/9 → 6/9，GA Gate `PASSED`
- 可靠指标：ROE 17.37%、CET1 14.2%、贷款增长 10%、存款增长 7%、信用损失准备 25 亿美元、信用卡净核销率 3.34%
- 主矛盾：`ROE versus Net Charge Off Rate`
- 缺口：统一口径 NIM、整体不良资产比率、整体效率比率

### XOM

- 覆盖率：0/9 → 4/9，GA Gate `LIMITED`
- 可靠指标：公司 Upstream 产量 470 万 BOE/day、Upstream earnings 222.47 亿美元、Downstream earnings 69.40 亿美元、季度 cash capex 68 亿美元
- 已纠正：不再把局部油砂项目 6.8 万桶/日当作公司总产量；不再把 32 万 BOE/day 当作天然气产量；不再把 earnings driver 的变动额当作分部利润
- 缺口：可靠 oil/gas 分项产量、realized oil/gas prices、FCF
- 结论：保持 `INDUSTRY_COVERAGE_LIMITED`，不为达到目标而猜测或硬编码

### TSLA

- 覆盖率：2/10 → 7/10，GA Gate `PASSED`
- 可靠指标：汽车销售收入 200.06 亿美元、汽车毛利率 21.1%、库存 137.52 亿美元、监管积分收入 1.46 亿美元、CapEx 指引超过 250 亿美元、能源销售收入 29.98 亿美元、能源收入增长 13%
- 已纠正：不再把 67.7 亿美元收入增量当交付量，不再把制造抵免当汽车收入，不再把库存减值当库存余额，不再把履约义务当监管积分收入，不再把收入增量当 ASP
- 缺口：绝对交付量、可靠 ASP、FCF；缺失值保持 N/A

## 4. 风险与叙事门禁

- 已按 FINANCIALS / ENERGY / AUTOMOTIVE / TECHNOLOGY / GENERAL 建立行业风险 taxonomy。
- 每条风险包含 `industry_relevance`，排序综合 severity、confidence、evidence quality 和 industry relevance。
- 跨行业风险继续保留，但低相关风险不会优先压过行业原生风险。
- Main Tension 要求 HIGH 以上、confidence ≥ 0.55、industry relevance ≥ 0.65 且有来源证据。
- 没有合格反方驱动时，严格输出：`No high-confidence opposing driver identified.`
- 通用分析占主导或行业指标不足时输出 `INDUSTRY_COVERAGE_LIMITED`，并降低 thesis confidence。

## 5. Provider 与降级检查

Fresh Run 结束时以下 Provider 全部为 `HEALTHY`：Financial Datasets、SEC、Tavily、DeepSeek、FRED、Yahoo Finance。

- GM：未复现 Provider error；当前限制来自可靠行业指标覆盖不足。
- F：缺失接口数据以 `EMPTY_DATA`/`PARTIAL_DATA` 语义处理，不再误报为系统错误。
- WMT：未复现 Provider error，4/4 完成。
- Research Engine 已对 TIMEOUT、UNREACHABLE、DEGRADED 进行有限重试；永久缺失不无限重试。

## 6. 验证结果

- 本地 Research 回归：33 passed
- 服务器核心 Research 回归：16 passed（最终能源改动相关集合）
- 早前服务器 Research 全集：32 passed
- 13 股票生产 Fresh Run：13/13 completed
- profiles correct：PASS
- factuality checks：PASS
- scenarios present：PASS
- providers healthy：PASS
- GA coverage passed：**FAIL**

## 7. 发布决定

根据“所有门禁通过后才升级版本”的规则：

- 保持 `research-v1.0-rc1`
- 不修改为 `research-v1.0`
- 不冻结 GA baseline
- 不新增付费数据源、Options、Flow、Social 或新研究模块

下一轮只应继续处理 XOM、GM、F 的公司级行业指标语义覆盖；在三者通过 GA Coverage Gate 之前，不宣布 V1.0 GA。
