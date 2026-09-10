'use client';
import { useState, type FormEvent } from 'react';
import { apiJson } from './lib/api';

type Quota = { status: string; month: string; message: string; used?: number; free_remaining?: number; paid_credits_estimate?: number; observed_at?: string; source?: string; official?: { plan_usage: number; paygo_usage: number } };
type Group = { provider: string; category: string; cached_tokens?: number; uncached_tokens?: number; output_tokens: number; unclassified_requests?: number; free_credits?: number; paid_credits?: number; token_costs?: Record<string, Record<string, number>> };
export type BillingReport = { tavily_quota?: Quota; pending_reasons?: Record<string, number>; by_provider: Group[] };
const labels: Record<string, string> = { input: '未缓存输入', cached_input: '缓存输入', output: '输出' };
const amount = (n: number) => n.toLocaleString('en-US', { maximumFractionDigits: 8 });

export function BillingPanel({ report, refresh }: { report: BillingReport | null; refresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [quota, setQuota] = useState<Quota | null>(null);
  const q = quota || report?.tavily_quota;
  async function action(path: string, body?: unknown) {
    setBusy(true); setMessage('');
    try {
      const result = await apiJson<{ message?: string } & Partial<Quota>>(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) });
      if (result.month) setQuota(result as Quota);
      setMessage(result.message || '配置已保存，请点击补算处理有依据的历史记录。');
      await refresh();
      setQuota(null);
    } catch (error) { setMessage(error instanceof Error ? error.message : '操作失败') }
    finally { setBusy(false) }
  }
  function calibrate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void action('/api/costs/tavily/calibrate', { used: Number(data.get('used')), confirmed: data.get('confirmed') === 'on' });
  }
  function mapping(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void action('/api/costs/model-mappings', { model: data.get('model'), target: data.get('target'), source: data.get('source'), effective_at: new Date(String(data.get('start'))).toISOString(), review_after: new Date(String(data.get('end'))).toISOString(), confirmed: data.get('confirmed') === 'on' });
  }
  return <section className="surface engine-card">
    <div className="surface-header"><div><h2>用量拆分与核算状态</h2><span>项目分项费用与账户额度分开显示</span></div><button disabled={busy} onClick={() => void action('/api/costs/reconcile')}>补算有依据的历史 LLM 记录</button></div>
    <div className="cost-note">
      {report?.by_provider.filter(g => g.category === 'llm').map(g => <div key={g.provider} className="billing-token-grid"><strong>{g.provider}</strong><span>未缓存输入 {amount(g.uncached_tokens || 0)} Tokens</span><span>缓存输入 {amount(g.cached_tokens || 0)} Tokens</span><span>输出 {amount(g.output_tokens)} Tokens</span><small>缓存分类不完整：{g.unclassified_requests || 0} 条（未计入前两项）</small>{Object.entries(g.token_costs || {}).map(([currency, costs]) => <p key={currency}>{currency} 已核算分项：{Object.entries(costs).map(([key, value]) => `${labels[key]} ${amount(value)}`).join(' · ')}</p>)}</div>)}
      {!!Object.keys(report?.pending_reasons || {}).length && <aside className="billing-reasons"><strong>为什么待核算？</strong>{Object.entries(report?.pending_reasons || {}).map(([reason, count]) => <p key={reason}>{reason}：{count} 条</p>)}</aside>}
      <h3>Tavily 本月账户额度</h3>
      <p>每月 1,000 免费 credits，超出 $0.008 / credit；不是每个请求固定收 $0.008。额度与调用记录分开存储，清空记录不会重置额度。</p>
      <p>{q?.month || '—'}（本地额度按 UTC 自然月隔离；新月需重新同步，不凭空发放免费额度）</p>
      <div className="billing-token-grid"><span>账户估计已用：{q?.used == null ? '未知' : amount(q.used)}</span><span>估计剩余免费：{q?.free_remaining == null ? '未知' : amount(q.free_remaining)}</span><span>账户估计超额：{q?.paid_credits_estimate == null ? '未知' : amount(q.paid_credits_estimate)}</span></div>
      {q?.official && <p>最近官方快照：套餐用量 {amount(q.official.plan_usage)} · 按量用量 {amount(q.official.paygo_usage)} credits</p>}
      {report?.by_provider.filter(g => g.category === 'search').map(g => <p key={g.provider}>本项目累计已分配：免费 {amount(g.free_credits || 0)} · 付费 {amount(g.paid_credits || 0)} credits（不是账户总量）</p>)}
      <p>{q?.message || '等待账户同步'}</p>{q?.observed_at && <p>快照：{new Date(q.observed_at).toLocaleString('zh-CN')} · {q.source}</p>}
      <button disabled={busy} onClick={() => void action('/api/costs/tavily/sync')}>{busy ? '处理中…' : '同步 Tavily 官方账户用量'}</button>
      <p>服务运行时每 5 分钟尝试同步。快照与后续本地调用用于估算免费抵扣；其他应用、并发或官方用量延迟可能导致差异，实际账单为准。快照之前的历史免费额度不倒推。</p>
      <details className="price-editor"><summary>无法同步？手动校准当前账户用量</summary><form onSubmit={calibrate}><label>账户本月累计已用 credits（免费＋付费）<input name="used" type="number" min="0" step="any" required /></label><label><input type="checkbox" name="confirmed" required />我确认这是当前账户用量，不是清空后项目调用数</label><button disabled={busy}>保存校准</button></form></details>
      <details className="price-editor"><summary>旧记录返回名不同？配置有依据的模型映射</summary><p>新调用会保留请求和返回模型名。历史缺少请求模型时，只有确认映射及有效期间后才能使用目标价格补算；保存后自动补算符合条件的记录，不会覆盖已核算费用。</p><form onSubmit={mapping}>
        <label>记录中的模型名<input name="model" defaultValue="deepseek-flash" required /></label><label>计价模型<input name="target" defaultValue="deepseek-v4-flash" required /></label>
        <label>有效起点（本机时间）<input name="start" type="datetime-local" required /></label><label>复核截止（本机时间）<input name="end" type="datetime-local" required /></label><label>确认依据<input name="source" required placeholder="实际请求配置／供应商确认" /></label><label><input type="checkbox" name="confirmed" required />已确认该期间实际使用目标模型及价格</label><button disabled={busy}>保存映射并补算</button>
      </form></details>
      {message && <p role="status">{message}</p>}
    </div>
  </section>;
}

export function UsageBreakdown({ event }: { event: { category: string; model?: string; requested_model?: string; pricing_model?: string; pricing_basis?: string; breakdown?: Record<string, { tokens: number; rate: number; amount: number }> | null; currency?: string | null; quota?: { free_credits: number; paid_credits: number; gross_amount: number }; quota_note?: string } }) {
  return <>{event.category === 'llm' && <p>原始返回模型：{event.model || '未记录'} · 请求模型：{event.requested_model || '历史未记录'} · 计价模型：{event.pricing_model || '未匹配'}{event.pricing_basis === 'requested_model' && '（按请求模型估算，返回名不同）'}{event.pricing_basis === 'confirmed_alias' && '（按已确认映射）'}</p>}{Object.entries(event.breakdown || {}).map(([key, row]) => <p key={key}>{labels[key]}：{amount(row.tokens)} Tokens × {amount(row.rate)} / 百万 = {amount(row.amount)} {event.currency}</p>)}{event.quota && <p>本次 credits：免费 {event.quota.free_credits} · 付费 {event.quota.paid_credits}；抵扣前估算 ${amount(event.quota.gross_amount)}</p>}{event.quota_note && <p>{event.quota_note}</p>}</>;
}
