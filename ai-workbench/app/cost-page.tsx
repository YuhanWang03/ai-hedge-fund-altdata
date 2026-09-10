'use client';
import { useState, type FormEvent } from 'react';
import { apiJson } from './lib/api';
import './cost-page.css';
import { BillingPanel, UsageBreakdown } from './billing-panel';

type Price = { automatic?: boolean; applied_period?: string; schedule?: { peak_rates: Record<string, number> }; currency: 'CNY' | 'USD'; id: string; provider: string; model: string; effective_at: string; review_after: string; source: string; rates: Record<string, number> };
type SyncStatus = { status: string; message: string; synced_at?: string; models?: string[] };
type Event = { requested_model?: string; pricing_model?: string; pricing_basis?: string; breakdown?: Record<string, { tokens: number; rate: number; amount: number }> | null; quota?: { free_credits: number; paid_credits: number; gross_amount: number }; quota_note?: string; amount: number | null; currency: 'CNY' | 'USD' | null; channel?: string; id: string; occurred_at: string; category: string; provider: string; model: string; ticker?: string; endpoint: string; source: string; state: string; status: string; reason: string; cost_usd: number | null; usage_basis: string; usage: { input_tokens?: number; output_tokens?: number; cached_tokens?: number; units?: number; search_depth?: string }; price: Price | null };
export type CostReport = { currencies: { currency: 'CNY' | 'USD'; today_amount: number; month_amount: number; total_amount: number }[]; timezone: string; today_cost_usd: number; month_cost_usd: number; total_cost_usd: number; total_requests: number; pending_requests: number; by_provider: { amounts: Record<string, number>; category: string; provider: string; cost_usd: number; requests: number; pending: number; input_tokens: number; output_tokens: number; credits: number }[]; prices: Price[]; recent: Event[] };
type Balance = { status: string; message: string; fetched_at?: string; balances?: { currency: string; total_balance: string; granted_balance: string; topped_up_balance: string }[] };
const money = (n: number | null | undefined, currency?: string | null) => n == null || !currency ? '待核算' : `${currency === 'CNY' ? '¥' : '$'}${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })} ${currency}`;
const time = (s: string) => new Date(s).toLocaleString('zh-CN', { timeZone: 'America/New_York', hour12: false }) + ' ET';
const category: Record<string, string> = { data: '数据查询', llm: 'LLM', search: '搜索' };
const channels: Record<string, string> = { web: '网页', telegram: 'Telegram', background: '后台任务', unknown: '来源未知' };
const endpoints: Record<string, string> = { financial_metrics: '财务指标', company_facts: '公司事实', news: '新闻', earnings: '财报', insider_trades: '内部人交易', prices: '股价', filings: 'SEC 文件', line_items: '财务报表', search: '网页搜索' };

export function CostPage({ report, loading, error, refresh }: { report: CostReport | null; loading: boolean; error: string; refresh: () => Promise<void> }) {
  const [filter, setFilter] = useState('all');
  const [balance, setBalance] = useState<Balance | null>(null);
  const [balanceBusy, setBalanceBusy] = useState(false);
  const [provider, setProvider] = useState('DeepSeek');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncResult, setSyncResult] = useState<SyncStatus | null>(null);
  const sync = syncResult || (report as (CostReport & { price_sync?: SyncStatus }) | null)?.price_sync;
  async function syncPrices() {
    setSyncBusy(true);
    try { setSyncResult(await apiJson<SyncStatus>('/api/costs/prices/sync', { method: 'POST' })); await refresh() }
    catch { setSyncResult({ status: 'error', message: '同步请求失败，请稍后重试；已有价格不受影响。' }) }
    finally { setSyncBusy(false) }
  }
  const llm = provider === 'DeepSeek' || provider === 'Other LLM';
  async function loadBalance() {
    setBalanceBusy(true);
    try { setBalance(await apiJson<Balance>('/api/costs/deepseek-balance')) }
    catch { setBalance({ status: 'unavailable', message: '余额查询失败，请稍后重试' }) }
    finally { setBalanceBusy(false) }
  }
  async function savePrice(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaving(true); setNotice('');
    const data = new FormData(event.currentTarget);
    try {
      const rates = Object.fromEntries((llm ? ['input', 'cached_input', 'output'] : ['unit']).map(k => [k, Number(data.get(k))]));
      await apiJson('/api/costs/prices', { method: 'POST', body: JSON.stringify({ provider, currency: data.get('currency'), model: data.get('model'), rates, source: data.get('source'), effective_at: new Date(String(data.get('effective'))).toISOString(), review_after: new Date(String(data.get('review'))).toISOString() }) });
      setNotice('价格版本已保存。历史金额和待核算记录保持不变，新调用按生效版本计费。'); await refresh();
    } catch (e) { setNotice(e instanceof Error ? e.message : '保存失败') }
    finally { setSaving(false) }
  }
  return <div className="cost-page unified-costs">
    <div className="page-heading"><div><span className="eyebrow">用量与费用</span><h1>花费</h1><p>统一查看网页、Telegram 与数据服务的调用费用</p></div><button className="run-button" disabled={loading} onClick={() => void refresh()}>{loading ? '刷新中…' : '刷新账本'}</button></div>
    {error && <p role="alert" className="quality-warning">{error}</p>}
    <div className="cost-summary-grid">
      {([['今日估算', 'today_amount'], ['本月估算', 'month_amount'], ['累计估算', 'total_amount']] as const).map(([label, field]) => <section className="surface" key={label}><span className="metric-label">{label}</span>{report ? report.currencies.map(c => <div className="currency-metric" key={c.currency}><span>{c.currency}</span><strong>{money(c[field], c.currency).replace(` ${c.currency}`, '')}</strong></div>) : <strong>—</strong>}</section>)}
      <section className="surface pending-metric"><span className="metric-label">待核算调用</span><strong>{report?.pending_requests ?? '—'}<small>条</small></strong><small>全部调用 {report?.total_requests ?? '—'} 条</small><button onClick={() => { setFilter('pending'); document.getElementById('cost-recent')?.scrollIntoView({ behavior: 'smooth' }) }}>查看待核算记录 →</button></section>
    </div>
    <p className="cost-basis">人民币与美元独立统计，不换算 · 按美东日期汇总 · 待核算金额不计入总额，实际扣费以供应商账单为准</p>
    <div className="engine-two-col cost-overview">
      <section className="surface engine-card"><div className="surface-header"><div><h2>按服务汇总</h2><span>累计估算及已记录用量</span></div></div><div className="cost-breakdown">{report?.by_provider.map(g => <div key={g.category + g.provider}><span>{category[g.category]} · {g.provider}<small>{g.category === 'llm' ? `输入 ${g.input_tokens.toLocaleString()} / 输出 ${g.output_tokens.toLocaleString()} Tokens` : g.category === 'search' ? `${g.credits.toLocaleString()} 已知 credits` : `${g.requests} 次请求`}</small></span><strong>{Object.entries(g.amounts).map(([code, amount]) => <span className="currency-line" key={code}>{money(amount, code)}</span>)}</strong><small>{g.pending} 条待核算</small></div>)}{!report?.by_provider.length && <p className="insufficient-note">尚无记录</p>}</div></section>
      <section className="surface engine-card"><div className="surface-header"><div><h2>DeepSeek 官方账户余额</h2><span>独立于本项目消费 · 最多每分钟查询一次</span></div><button disabled={balanceBusy} onClick={() => void loadBalance()}>{balanceBusy ? '查询中…' : '查询余额'}</button></div><div className="cost-note">{balance?.balances?.map(b => <p key={b.currency}><strong>{b.total_balance} {b.currency}</strong> 可用余额<br/>充值余额 {b.topped_up_balance} · 赠金余额 {b.granted_balance}</p>)}<p>{balance?.message || '使用服务器配置的 API Key 查询官方余额；密钥不发送到浏览器。余额变动不用于反推项目消费。'}</p>{balance?.fetched_at && <small>查询于 {time(balance.fetched_at)}</small>}</div></section>
    </div>
    <BillingPanel report={report} refresh={refresh}/>
    <section id="cost-recent" className="surface engine-card"><div className="surface-header"><div><h2>最近调用</h2><span>最新 100 条 · 点击记录展开明细</span></div><select aria-label="筛选费用类型" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部类型</option><option value="data">数据查询</option><option value="llm">LLM</option><option value="search">搜索</option><option value="pending">待核算</option></select></div>
      <div className="usage-columns" aria-hidden="true"><span>来源 / 时间</span><span>服务 / 模型</span><span>估算费用</span></div>
      <div className="usage-list">{report?.recent.filter(e => filter === 'all' || (filter === 'pending' ? e.status === 'pending' : e.category === filter)).map(e => <details key={e.id}><summary><span><span className="cost-channel">{channels[e.channel || 'unknown'] || '来源未知'}</span>{time(e.occurred_at)}<small>{e.ticker || e.source || '—'}</small></span><span>{e.provider}<small>{category[e.category]} · {e.category === 'llm' ? (e.pricing_model || e.requested_model || e.model) : (endpoints[e.model] || e.model)}</small></span><strong className={e.status === 'pending' ? 'pending-cost' : ''}>{money(e.amount, e.currency)}</strong></summary><div className="usage-detail"><UsageBreakdown event={e}/><p>{e.state === 'failed' ? '请求失败／计费待核对' : '请求已返回'} · {e.usage_basis === 'reported' ? '接口返回用量' : e.usage_basis === 'legacy' ? '旧账本记录' : '用量未知'}{e.reason && ` · ${e.reason}`}</p><p>{e.category === 'llm' ? `输入 ${e.usage.input_tokens ?? '未知'} · 缓存命中 ${e.usage.cached_tokens ?? '未知'} · 输出 ${e.usage.output_tokens ?? '未知'} Tokens` : `${e.category === 'search' ? 'Credits' : '请求数'}：${e.usage.units ?? '未知'}${e.usage.search_depth ? ` · 模式 ${e.usage.search_depth}` : ''}`}</p>{e.price ? <><p>价格版本：{e.price.id}</p><p>采用单价（{e.price.currency || 'USD'}）：{Object.entries(e.price.rates).map(([k,v]) => `${({ input: '输入/百万 Tokens', cached_input: '缓存输入/百万 Tokens', output: '输出/百万 Tokens', unit: e.category === 'search' ? '每 credit' : '每请求' } as Record<string,string>)[k] || k} ${v}`).join('；')}</p><p>来源：{e.price.source}</p></> : <p>未记录价格版本；旧账本金额予以保留。</p>}</div></details>)}{!report?.recent.length && <p className="insufficient-note">新用量将在调用后自动记录，不补造历史 LLM 或搜索费用。</p>}</div>
    </section>
    <details className="surface engine-card cost-settings"><summary className="surface-header"><div><h2>价格与计费设置</h2><span>官方价格同步 · 手动单价 · 历史版本</span></div><span className={`sync-pill ${sync?.status === 'ok' ? 'ok' : ''}`}>{sync?.status === 'ok' ? '最近同步成功' : sync?.status === 'error' ? '同步需关注' : '等待同步'} · 展开</span></summary>
      <div className="cost-note">
        <h3>DeepSeek 官方价格自动填写</h3>
        <p>服务启动时检查，此后每 6 小时同步一次。自动识别模型、人民币单价及北京时间高峰／空闲时段；连续 24 小时未成功复核则暂停金额估算。</p>
        <button disabled={syncBusy} onClick={() => void syncPrices()}>{syncBusy ? '同步中…' : '立即同步官方价格'}</button>
        <p role="status">{sync?.message || '尚未取得同步状态'}</p>
        {sync?.synced_at && <small>最近成功：{time(sync.synced_at)} · 手动同步间隔至少 1 分钟</small>}
        <p>价格从采集时刻起适用，不回填此前的待核算费用。Financial Datasets 沿用固定端点配置；Tavily 按实际套餐单价在下方填写。</p>
        {report?.prices.filter((p, i, all) => p.automatic && all.findIndex(q => q.automatic && q.model === p.model) === i).map(p => <p key={p.id}><strong>{p.model}</strong><br/>每百万 Tokens（人民币）：未缓存输入 / 缓存输入 / 输出<br/>空闲：{p.rates.input} / {p.rates.cached_input} / {p.rates.output}{p.schedule && <>；高峰：{p.schedule.peak_rates.input} / {p.schedule.peak_rates.cached_input} / {p.schedule.peak_rates.output}</>}</p>)}
        <a href="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/" target="_blank" rel="noreferrer">查看官方价格与时段规则 ↗</a>
      </div>
      <details className="price-editor"><summary>手动新增／更新价格版本（自动同步会采用后续官方版本）</summary><form onSubmit={event => void savePrice(event)}>
        <label>供应商<select value={provider} onChange={e => setProvider(e.target.value)}>{['DeepSeek', 'Tavily', 'Financial Datasets', 'Other LLM'].map(p => <option key={p}>{p}</option>)}</select></label>
        <label>计价币种<select name="currency" required defaultValue="USD"><option value="USD">美元 USD</option><option value="CNY">人民币 CNY</option></select></label>
        <label>精确模型名／端点名<input key={provider} name="model" required maxLength={120} defaultValue={provider === 'Tavily' ? 'search' : ''} placeholder={llm ? '与调用记录返回的模型名一致' : '例如 financial_metrics'} /></label>
        {(llm ? [['input','未缓存输入 / 百万 Tokens'],['cached_input','缓存输入 / 百万 Tokens'],['output','输出 / 百万 Tokens']] : [['unit',provider === 'Tavily' ? '每 credit（套餐折算价）' : '每次请求']]).map(([k,label]) => <label key={k}>{label}<input name={k} type="number" min="0" max="100000" step="any" required /></label>)}
        <label>生效时间（本机时区）<input name="effective" type="datetime-local" required /></label><label>价格复核期限（本机时区）<input name="review" type="datetime-local" required /></label>
        <label className="price-source">价格来源／套餐说明<input name="source" required maxLength={500} placeholder="官方价格页面、核对日期及适用套餐" /></label><button disabled={saving} type="submit">{saving ? '保存中…' : '保存新版本'}</button>
      </form>{notice && <p role="status">{notice}</p>}</details>
      <div className="cost-price-list">{report?.prices.map(p => <div key={p.id}><strong>{p.provider} · {p.model} · {p.currency || 'USD'}</strong><span>{time(p.effective_at)} 生效 → {time(p.review_after)} 前复核</span><small>{p.source}</small></div>)}{!report?.prices.length && <p className="insufficient-note">尚未配置价格版本。FD 沿用既有端点配置；LLM 和 Tavily 先记录用量，金额待核算。</p>}</div>
      <div className="cost-note"><p>每笔调用冻结当时采用的单价；新增价格不会自动改写历史；可主动补算有可靠历史价格和完整用量的待核算 LLM 记录，原记录留存审计。没有用量、没有价格或价格过期时，显示待核算。LLM 即使后续解析失败，只要 API 已返回用量仍会记录。</p><p>Tavily 的 credits 按接口返回值记录；有效账户校准之后的调用按剩余额度抵扣。快照以前的历史额度、其他套餐、赠金和退款不倒推；官方账单才是最终扣费依据。SDK 内部重试若未返回用量，无法逐次核算。当前覆盖项目中的非流式 ChatDeepSeek、Agent Chat API 和 Tavily search，不含嵌入模型或其他未接入服务。</p><p><a href="https://api-docs.deepseek.com/quick_start/pricing" target="_blank" rel="noreferrer">核对 DeepSeek 官方价格 ↗</a> · <a href="https://docs.tavily.com/documentation/api-credits" target="_blank" rel="noreferrer">核对 Tavily 价格 ↗</a></p></div>
    </details>
  </div>;
}
