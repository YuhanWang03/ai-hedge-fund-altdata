'use client';
import { useState, type FormEvent } from 'react';
import { apiJson } from './lib/api';
import './cost-page.css';

type Price = { id: string; provider: string; model: string; effective_at: string; review_after: string; source: string; rates: Record<string, number> };
type Event = { channel?: string; id: string; occurred_at: string; category: string; provider: string; model: string; ticker?: string; endpoint: string; source: string; state: string; status: string; reason: string; cost_usd: number | null; usage_basis: string; usage: { input_tokens?: number; output_tokens?: number; cached_tokens?: number; units?: number; search_depth?: string }; price: Price | null };
export type CostReport = { timezone: string; today_cost_usd: number; month_cost_usd: number; total_cost_usd: number; total_requests: number; pending_requests: number; by_provider: { category: string; provider: string; cost_usd: number; requests: number; pending: number; input_tokens: number; output_tokens: number; credits: number }[]; prices: Price[]; recent: Event[] };
type Balance = { status: string; message: string; fetched_at?: string; balances?: { currency: string; total_balance: string; granted_balance: string; topped_up_balance: string }[] };
const money = (n: number | null | undefined) => n == null ? '待核算' : `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`;
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
      await apiJson('/api/costs/prices', { method: 'POST', body: JSON.stringify({ provider, model: data.get('model'), rates, source: data.get('source'), effective_at: new Date(String(data.get('effective'))).toISOString(), review_after: new Date(String(data.get('review'))).toISOString() }) });
      setNotice('价格版本已保存。历史金额和待核算记录保持不变，新调用按生效版本计费。'); await refresh();
    } catch (e) { setNotice(e instanceof Error ? e.message : '保存失败') }
    finally { setSaving(false) }
  }
  return <div className="cost-page unified-costs">
    <div className="page-heading"><div><span className="eyebrow">统一用量与费用账本</span><h1>花费</h1><p>数据查询 · LLM · Tavily 搜索。金额为估算，不等于供应商实际扣费。</p></div><button className="run-button" disabled={loading} onClick={() => void refresh()}>{loading ? '刷新中…' : '刷新账本'}</button></div>
    {error && <p role="alert" className="quality-warning">{error}</p>}
    <div className="cost-summary-grid">
      {([['今日已核价估算', report?.today_cost_usd], ['本月已核价估算', report?.month_cost_usd], ['累计已核价估算', report?.total_cost_usd]] as const).map(([label, value]) => <section className="surface" key={label}><span>{label}</span><strong>{report ? money(value) : '—'}</strong><small>按美东时间统计</small></section>)}
      <section className="surface"><span>待核算调用</span><strong>{report?.pending_requests ?? '—'}</strong><small>共 {report?.total_requests ?? '—'} 条记录 · 待核算金额未计入总额</small></section>
    </div>
    <div className="engine-two-col">
      <section className="surface engine-card"><div className="surface-header"><div><h2>按服务汇总</h2><span>累计估算及已记录用量</span></div></div><div className="cost-breakdown">{report?.by_provider.map(g => <div key={g.category + g.provider}><span>{category[g.category]} · {g.provider}<small>{g.category === 'llm' ? `输入 ${g.input_tokens.toLocaleString()} / 输出 ${g.output_tokens.toLocaleString()} Tokens` : g.category === 'search' ? `${g.credits.toLocaleString()} 已知 credits` : `${g.requests} 次请求`}</small></span><strong>{money(g.cost_usd)}</strong><small>{g.pending} 条待核算</small></div>)}{!report?.by_provider.length && <p className="insufficient-note">尚无记录</p>}</div></section>
      <section className="surface engine-card"><div className="surface-header"><div><h2>DeepSeek 官方账户余额</h2><span>独立于本项目消费 · 最多每分钟查询一次</span></div><button disabled={balanceBusy} onClick={() => void loadBalance()}>{balanceBusy ? '查询中…' : '查询余额'}</button></div><div className="cost-note">{balance?.balances?.map(b => <p key={b.currency}><strong>{b.total_balance} {b.currency}</strong> 可用余额<br/>充值余额 {b.topped_up_balance} · 赠金余额 {b.granted_balance}</p>)}<p>{balance?.message || '使用服务器配置的 API Key 查询官方余额；密钥不发送到浏览器。余额变动不用于反推项目消费。'}</p>{balance?.fetched_at && <small>查询于 {time(balance.fetched_at)}</small>}</div></section>
    </div>
    <section className="surface engine-card"><div className="surface-header"><div><h2>最近调用</h2><span>最新 100 条 · 展开查看用量和价格快照</span></div><select aria-label="筛选费用类型" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部类型</option><option value="data">数据查询</option><option value="llm">LLM</option><option value="search">搜索</option><option value="pending">待核算</option></select></div>
      <div className="usage-list">{report?.recent.filter(e => filter === 'all' || (filter === 'pending' ? e.status === 'pending' : e.category === filter)).map(e => <details key={e.id}><summary><span><span className="cost-channel">{channels[e.channel || 'unknown'] || '来源未知'}</span>{time(e.occurred_at)}<small>{e.ticker || e.source || '—'}</small></span><span>{e.provider}<small>{category[e.category]} · {endpoints[e.model] || e.model}</small></span><strong className={e.status === 'pending' ? 'pending-cost' : ''}>{money(e.cost_usd)}</strong></summary><div className="usage-detail"><p>{e.state === 'failed' ? '请求失败／计费待核对' : '请求已返回'} · {e.usage_basis === 'reported' ? '接口返回用量' : e.usage_basis === 'legacy' ? '旧账本记录' : '用量未知'}{e.reason && ` · ${e.reason}`}</p><p>{e.category === 'llm' ? `输入 ${e.usage.input_tokens ?? '未知'} · 缓存命中 ${e.usage.cached_tokens ?? '未知'} · 输出 ${e.usage.output_tokens ?? '未知'} Tokens` : `${e.category === 'search' ? 'Credits' : '请求数'}：${e.usage.units ?? '未知'}${e.usage.search_depth ? ` · 模式 ${e.usage.search_depth}` : ''}`}</p>{e.price ? <><p>价格版本：{e.price.id}</p><p>采用单价（USD）：{Object.entries(e.price.rates).map(([k,v]) => `${({ input: '输入/百万 Tokens', cached_input: '缓存输入/百万 Tokens', output: '输出/百万 Tokens', unit: e.category === 'search' ? '每 credit' : '每请求' } as Record<string,string>)[k] || k} ${v}`).join('；')}</p><p>来源：{e.price.source}</p></> : <p>未记录价格版本；旧账本金额予以保留。</p>}</div></details>)}{!report?.recent.length && <p className="insufficient-note">新用量将在调用后自动记录，不补造历史 LLM 或搜索费用。</p>}</div>
    </section>
    <section className="surface engine-card"><div className="surface-header"><div><h2>价格版本与核对</h2><span>只新增版本，不改写历史费用 · 单价币种 USD</span></div></div>
      <details className="price-editor"><summary>新增／更新价格版本</summary><form onSubmit={event => void savePrice(event)}>
        <label>供应商<select value={provider} onChange={e => setProvider(e.target.value)}>{['DeepSeek', 'Tavily', 'Financial Datasets', 'Other LLM'].map(p => <option key={p}>{p}</option>)}</select></label>
        <label>精确模型名／端点名<input key={provider} name="model" required maxLength={120} defaultValue={provider === 'Tavily' ? 'search' : ''} placeholder={llm ? '与调用记录返回的模型名一致' : '例如 financial_metrics'} /></label>
        {(llm ? [['input','未缓存输入 / 百万 Tokens'],['cached_input','缓存输入 / 百万 Tokens'],['output','输出 / 百万 Tokens']] : [['unit',provider === 'Tavily' ? '每 credit（套餐折算价）' : '每次请求']]).map(([k,label]) => <label key={k}>{label}<input name={k} type="number" min="0" max="100000" step="any" required /></label>)}
        <label>生效时间（本机时区）<input name="effective" type="datetime-local" required /></label><label>价格复核期限（本机时区）<input name="review" type="datetime-local" required /></label>
        <label className="price-source">价格来源／套餐说明<input name="source" required maxLength={500} placeholder="官方价格页面、核对日期及适用套餐" /></label><button disabled={saving} type="submit">{saving ? '保存中…' : '保存新版本'}</button>
      </form>{notice && <p role="status">{notice}</p>}</details>
      <div className="cost-price-list">{report?.prices.map(p => <div key={p.id}><strong>{p.provider} · {p.model}</strong><span>{time(p.effective_at)} 生效 → {time(p.review_after)} 前复核</span><small>{p.source}</small></div>)}{!report?.prices.length && <p className="insufficient-note">尚未配置价格版本。FD 沿用既有端点配置；LLM 和 Tavily 先记录用量，金额待核算。</p>}</div>
      <div className="cost-note"><p>每笔调用冻结当时采用的单价；新增价格不会重算历史。没有用量、没有价格或价格过期时，显示待核算。LLM 即使后续解析失败，只要 API 已返回用量仍会记录。</p><p>Tavily 的 credits 按接口返回值记录。套餐费用、免费额度、赠金和退款不在此自动抵扣；官方账单才是最终扣费依据。SDK 内部重试若未返回用量，无法逐次核算。当前覆盖项目中的非流式 ChatDeepSeek、Agent Chat API 和 Tavily search，不含嵌入模型或其他未接入服务。</p><p><a href="https://api-docs.deepseek.com/quick_start/pricing" target="_blank" rel="noreferrer">核对 DeepSeek 官方价格 ↗</a> · <a href="https://docs.tavily.com/documentation/api-credits" target="_blank" rel="noreferrer">核对 Tavily 价格 ↗</a></p></div>
    </section>
  </div>;
}
