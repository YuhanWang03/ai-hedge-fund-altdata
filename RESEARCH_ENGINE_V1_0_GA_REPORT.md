# RESEARCH ENGINE V1.0 GA REPORT

生成日期：2026-09-06  
发布版本：`research-v1.0`  
发布状态：**RESEARCH ENGINE V1.0 — FEATURE FREEZE**

## 1. XOM Final Diagnosis

- 已修复一个事实性 Parser 缺陷：全球石油供应情景中的 `100 million barrels/day` 不再被识别为 XOM 公司油产量。
- SEC 公司级合计表可稳定识别总产量和天然气产量；最新 Fresh Run 显示总产量约 `4.514 million BOE/day`，天然气产量 `8,442 MMcf/day`。
- Upstream Earnings、Downstream Earnings、CapEx 均有可追踪证据；FCF 缺失时保持 `N/A`。
- 单独油产量、实现油价、实现气价等非核心字段覆盖不足时不推断、不使用项目级或市场级数字替代。
- 最终分类：`STANDARD`；必需指标覆盖 `4/5 = 80%`，可选指标覆盖 `3/9 = 33.3%`。

## 2. GM / F Final Diagnosis

GM：

- 已支持公司级 Automotive Operations 批发量合计表；最新季度公司级 Wholesale Vehicle Sales 为 `990,000`。
- 已阻止地区销量被误认为公司总量。
- 已清除错误的 `$92` 汽车收入和无单位 `$15,950` 库存值；没有明确金额单位时保持 `N/A`。
- 最终分类：`LIMITED`；必需指标覆盖 `3/6 = 50%`。

Ford：

- Ford Blue、Model e、Ford Pro 的分部销量和 EBIT Margin 不再静默合并成 Tesla-style Deliveries 或统一汽车毛利率。
- 分部披露只保留其原始定义；公司级字段缺失时保持 `N/A`。
- 最终分类：`LIMITED`；必需指标覆盖 `3/6 = 50%`，可选指标覆盖 `1/5 = 20%`。

## 3. Required vs Optional Metrics

| Industry | Required Metrics | Optional Metrics |
|---|---|---|
| FINANCIALS | ROE、NIM、CET1、Loan Growth、Deposit Growth、Credit Quality | Provision、Net Charge-off Rate、Efficiency Ratio |
| ENERGY | Production、Upstream Earnings、Downstream Earnings、CapEx、FCF | Oil/Gas Production、Realized Prices、Chemical Earnings、Reserve/Production、Breakeven、WTI、Natural Gas |
| AUTOMOTIVE | Vehicle Volume、Automotive Revenue、Automotive Margin、Inventory、CapEx、FCF | ASP、Regulatory Credits、Energy Revenue/Growth、Segment Margin |
| TECHNOLOGY | Revenue Growth、Gross Margin、Operating Margin、ROIC | FCF、R&D、SBC |
| GENERAL | Revenue Growth、Gross Margin、Operating Margin、ROIC | FCF |

公司特有或不适用指标不再降低另一家公司的 Required Coverage。

## 4. Research Support Tier

支持等级由确定性规则生成，不由 LLM 判断：

- `FULL`：必需指标、核心模块、通用研究和证据质量均充分。
- `STANDARD`：核心研究可靠，必需行业指标达到可用标准。
- `LIMITED`：结果仍可发布，但必须降低置信度并明确行业证据边界。
- `INSUFFICIENT`：核心模块、通用研究或证据质量不足，结果不可作为正常研究结果发布。

## 5. Coverage Gate v2

Coverage Gate v2 同时考虑：

- Required Metric Coverage
- Optional Metric Coverage
- Industry Finding Count
- Evidence Verification
- General Research Coverage
- Core Module Availability

门禁结果为 `PASSED`、`LIMITED`、`FAILED`。其中 `LIMITED` 是正常、可发布的降级状态；只有证据安全或核心研究严重不足时才为 `FAILED`。

## 6. Final 13-stock Regression

| Ticker | Industry | Required | Optional | Support Tier | Confidence | Coverage Gate | Factuality | Provider Errors |
|---|---|---:|---:|---|---:|---|---|---|
| AAPL | TECHNOLOGY | 100% | 33.3% | FULL | 87 | PASSED | PASSED | 无 |
| NVDA | TECHNOLOGY | 100% | 33.3% | FULL | 87 | PASSED | PASSED | 无 |
| MSFT | TECHNOLOGY | 100% | 33.3% | FULL | 87 | PASSED | PASSED | 无 |
| JPM | FINANCIALS | 66.7% | 66.7% | STANDARD | 73 | PASSED | PASSED | EMPTY_DATA（模块级） |
| BAC | FINANCIALS | 66.7% | 66.7% | STANDARD | 87 | PASSED | PASSED | 无 |
| XOM | ENERGY | 80% | 33.3% | STANDARD | 74 | PASSED | PASSED | EMPTY_DATA（模块级） |
| CVX | ENERGY | 100% | 33.3% | FULL | 88 | PASSED | PASSED | 无 |
| TSLA | AUTOMOTIVE | 66.7% | 60% | STANDARD | 75 | PASSED | PASSED | EMPTY_DATA（模块级） |
| GM | AUTOMOTIVE | 50% | 0% | LIMITED | 74 | LIMITED | PASSED | 无 |
| F | AUTOMOTIVE | 50% | 20% | LIMITED | 74 | LIMITED | PASSED | 无 |
| WMT | GENERAL | 100% | 100% | FULL | 87 | PASSED | PASSED | 无 |
| KO | GENERAL | 100% | 100% | FULL | 87 | PASSED | PASSED | 无 |
| CAT | GENERAL | 100% | 100% | FULL | 84 | PASSED | PASSED | 无 |

13/13 均完成或优雅降级；13/13 行业分类正确；13/13 情景输出存在。

## 7. Factuality Result

- 13/13 `Factuality = PASSED`。
- 13/13 未发现 unsupported financial claims。
- Bull/Base/Bear 仅引用结构化 Finding/Evidence。
- Snapshot、Finding、Evidence、Source 和原始披露定义可追踪。
- Industry Metric 现统一保留 `metric_name`、`normalized_name`、`value`、`unit`、`period`、`source`、`source_label`、`definition`、`confidence`。

## 8. Remaining Data Limitations

- GM/F 的公司级 Automotive Revenue、Automotive Margin 或 Inventory 在当前已采集材料中并非始终具有可稳定解析的定义和单位。
- XOM 的单独油产量、实现价格和 FCF 并非每次都能从同一报告期材料获得。
- `EMPTY_DATA` 表示某个模块在本次调用中没有返回记录，不等同于 Provider 不健康；最终健康检查中 Financial Datasets、SEC、Tavily、DeepSeek、FRED、Yahoo Finance 均为 `HEALTHY`。
- 系统将这些限制显示为 LIMITED/N/A，不使用 Generic Findings 假装行业研究完整。

## 9. V1.1 Backlog（仅记录，未实现）

- Historical Analyst Consensus
- Options
- Unusual Options
- Block Trade
- Real-time Institutional Flow
- Reddit/X Sentiment
- More Industry Profiles
- Advanced Factor Models

## 10. GA Decision

所有 V1.0 GA 核心门禁通过：

- Engine 稳定运行
- Provider Failure Handling 正常
- 数据缺失和 Coverage Limitation 明确
- Factuality Guard 通过
- 无不受支持财务断言
- Evidence Traceability 正常
- LIMITED 股票能够保守生成研究结果
- 无已知严重生产 Bug

最终决定：`research-v1.0-rc1` 正式晋升为 **`research-v1.0`**。

Research Engine V1.0 自此进入 **FEATURE FREEZE**。后续仅接受 Bug、Factuality Issue、Critical Data Issue；新研究模块、技术指标、风险类别、基本面类别和替代数据需求进入 V1.1 Backlog。
