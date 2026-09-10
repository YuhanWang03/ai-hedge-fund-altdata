'use client';

import { useId, useRef } from 'react';
import './research-help.css';

// Documentation of the existing research engine, not model-generated run output.
export const RESEARCH_HELP = {
  stock: {
    title: '个股工作台', purpose: '把一只股票的研究结果集中在同一处，快速了解投资论点、驱动因素、风险和数据完整程度。',
    features: '汇总模块评分、研究论点与变化，展示各模块完成状态、摘要、缓存、覆盖率和错误，并支持重试。',
    method: '研究引擎按依赖关系运行基本面、估值、财报、预期、机构、资金流、技术面、催化剂、SEC、宏观、产业链与风险模块，共享标准化数据和缓存，再聚合结果。单个模块失败不会让其他结果消失。',
    data: '按实际运行模块使用 Financial Datasets 财务及公司数据、Yahoo Finance 价量与预期、SEC 文件、本地 13F / ETF 数据、宏观数据和网页搜索证据。中文翻译及部分解释可调用配置的 LLM。',
    limit: '评分是系统规则的研究辅助结果，不是投资收益预测。覆盖率只反映模块所需字段的可用程度，不代表数据准确率或对公司的全面覆盖。',
  },
  fundamentals: {
    title: '公司与基本面', purpose: '理解公司做什么、增长如何、盈利质量和偿债能力是否稳健。',
    features: '展示公司概况、营收与 EPS 增长、利润率、ROIC / ROE / ROA、季度趋势、财务健康以及可用股东价值指标。',
    method: '读取公司事实和财务指标，对同季度申报去重，计算或整理增长、盈利和杠杆指标，并按确定性规则形成评分和财务健康标签。',
    data: 'Financial Datasets 的公司事实、滚动财务指标；共享研究结果中可用的季度财报数据。公司介绍可经过中文本地化处理。',
    limit: '季度趋势和股东价值字段依赖本次实际取得的数据；未返回的股权激励、回购等项目显示缺失，不补造数值。',
  },
  valuation: {
    title: '估值与同业', purpose: '查看当前估值水平，并与可用历史记录和同行候选进行对照。',
    features: '展示市盈率、预期市盈率、PEG、市销率、企业价值倍数、自由现金流收益率，以及历史中位数、百分位、样本数和同行候选。',
    method: '从财务指标提取当前倍数，对实际可用的历史样本计算中位数及位置。同行区域展示候选集合，不会仅为填满表格而重复请求全部同行财务。',
    data: 'Financial Datasets 公司事实与历史滚动财务指标，以及研究引擎中可用的同行候选信息。',
    limit: '历史样本不等于完整五年历史；同行候选不等于已完成逐家公司可比估值。低估值也不自动等于低风险。',
  },
  earnings: {
    title: '财报与 SEC', purpose: '对照财报实际表现与市场预期，并追溯公司向监管机构提交的披露材料。',
    features: '展示近期财报、EPS 和营收超预期、下次财报日期、10-K / 10-Q / 8-K 文件索引、结构化发现和风险因素变化，并提供可用出处链接。',
    method: '整合财报数据和 SEC 索引，读取可取得的申报正文，以结构化解析和文本规则提取业务、管理层讨论、风险与指引，再进行中文展示。',
    data: 'Financial Datasets 财报历史及文件元数据、SEC EDGAR 申报正文、Yahoo Finance 财报日历；中文文本可能通过 LLM 翻译。',
    limit: '正文提取可能包含截断、目录或匹配误差；风险披露增减不等同于实际风险必然升降。重要结论应点击原文核查。',
  },
  expectations: {
    title: '预期与催化剂', purpose: '区分分析师市场预期、公司管理层表述和可能影响股价的事件。',
    features: '展示当前一致预期、历史业绩对照、预期修正可用性、盈利动量、管理层指引候选以及催化剂时间线。',
    method: '先尝试取得一致预期和财报日历，再对照历史实际值；从申报材料中整理指引候选，过滤通用免责声明并合并重复表述，结合财报、新闻、SEC 和宏观事件形成时间线。',
    data: 'Yahoo Finance 预期与日历、Financial Datasets 财报、SEC 正文、项目新闻服务（Financial Datasets / Tavily，按配置及可用性）和宏观事件日历。',
    limit: '没有同一目标报告期的历史预期快照，就不能计算 30 / 60 / 90 日修正。未提取到指引不表示公司从未发布指引，事件方向也不是股价预测。',
  },
  institutional: {
    title: '机构与内部人', purpose: '观察已跟踪机构、ETF 和公司内部人的持仓及交易变化。',
    features: '展示机构持仓、13F 季度变动、ETF 持仓暴露、内部人交易及相关规则摘要。',
    method: '读取本地已采集的机构最近两期申报，比较股数和市值；读取各 ETF 最新快照，并标准化内部人交易记录。',
    data: '项目本地 SEC 13F 机构档案库、ETF 快照库，以及 Financial Datasets 内部人交易数据。',
    limit: '只覆盖已跟踪并已采集的机构和 ETF，13F 存在披露滞后；持仓市值变化包含价格影响，不能直接当成净买入资金。',
  },
  moneyflow: {
    title: '资金流', purpose: '通过成交量与价格的关系观察资金累积、派发和潜在量价背离。',
    features: '展示成交量、相对成交量、CMF20、OBV、累积 / 派发指标，并整合 /flow 的多轴读数、背离信号、图表及可用多空解释。',
    method: '复用本次研究的日线 OHLCV 计算指标，调用已有 /flow 背离检测规则；历史不足或价量无效时不生成结论。触发信号且模型已配置时，可调用 LLM 生成多空解释。',
    data: '研究引擎配置的日线价量源（默认 Yahoo Finance）；/flow 命令本身使用 Financial Datasets。解释功能可使用 DeepSeek。',
    limit: '这些是价量代理指标，不是真实账户资金流水。机构和 ETF 净流数据缺失时不会用技术指标冒充；不同数据源或截止日期可能导致与 /flow 结果不同。',
  },
  macro: {
    title: '宏观环境', purpose: '了解个股所处的利率、波动率和经济数据环境。',
    features: '展示可用宏观快照、关键市场指标、环境摘要及未来经济数据发布时间。',
    method: '复用项目已有宏观管线生成快照，并读取未来 45 天的经济事件日历；按指标规则整理环境判断。股票代码作为研究上下文，不会把全国宏观数据变成公司专属指标。',
    data: '项目 FRED / Yahoo Finance 宏观数据管线，以及已有经济数据发布日历。',
    limit: '各宏观指标的更新时间和频率不同；日历可能调整，环境标签不表示已经证实对某只股票的因果影响。',
  },
  chain: {
    title: '产业链', purpose: '发现并查看当前公司与供应商、客户、同行和间接受益方之间的候选关系。',
    features: '展示关系概况、公司产业关系图、候选说明、公司身份与关系证据状态、搜索原文和来源，并支持重新验证。',
    method: '复用横向发现流程生成候选，分别核验公司身份和关系证据，结合已有缓存与网页检索结果保存关系。示意图按本次返回的数据生成，不为没有结果的股票填充其他公司的模板。',
    data: '已有关系存储、Financial Datasets / Yahoo Finance 公司身份数据、Tavily 网页搜索证据；候选发现可使用配置的 LLM。',
    limit: '公司身份确认不等于产业关系证实；搜索共同提及也不能证明交易关系。图中候选说明和连线类别需结合原文证据理解。',
  },
  risk: {
    title: '风险雷达', purpose: '把分散在各研究模块中的风险信号集中展示，便于查看证据和缺失项。',
    features: '汇总估值、财务、会计、供应链、客户集中度、管理层、宏观和事件等八类风险，并展示等级、原因、置信度和证据。',
    method: '复用基本面、估值、SEC、机构、宏观、产业链和催化剂的结果，通过指标阈值、文本线索和关系数量等规则分级；证据不足时标记未知。',
    data: '上游模块的财务指标、SEC 披露、机构记录、宏观快照、关系证据及事件信息；不另有一个全覆盖的风险数据库。',
    limit: '风险等级和置信度是规则输出，不是发生概率。未发现关键词不代表没有风险；客户关系数量不能代替真实营收集中度。',
  },
} as const;

export function ResearchHelp({ tool }: { tool: keyof typeof RESEARCH_HELP }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const help = RESEARCH_HELP[tool];
  return <>
    <button ref={trigger} type="button" className="research-info-button" aria-label={`了解${help.title}`} aria-haspopup="dialog" onClick={() => dialog.current?.showModal()}><span aria-hidden="true">i</span></button>
    <dialog ref={dialog} className="research-help-dialog" aria-labelledby={id} onClose={() => trigger.current?.focus()} onClick={event => {
      if (event.target !== event.currentTarget) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.currentTarget.close();
    }}>
      <header><div><span>研究工具说明</span><h2 id={id}>{help.title}</h2></div><button type="button" autoFocus aria-label="关闭说明" onClick={() => dialog.current?.close()}>×</button></header>
      <div className="research-help-content">
        {([['页面用途', help.purpose], ['已实现功能', help.features], ['如何实现', help.method], ['使用哪些数据', help.data]] as const).map(([heading, text], index) => <section key={heading}><h3><span aria-hidden="true">0{index + 1}</span>{heading}</h3><p>{text}</p></section>)}
        <aside><strong>使用边界</strong><p>{help.limit}</p></aside>
      </div>
      <footer>这是功能说明，不代表本次已调用全部数据源。实际使用情况以页面来源、时间和数据质量为准。打开说明不会发起研究或调用付费 API。</footer>
    </dialog>
  </>;
}
