'use client';

/* 实验室 — 一条主线：筛选 → 委员会 → 回测 → 观察 → 批准（加入 Watchlist）。
   每个工具左侧是真实可调的参数，右侧是该工具自己的结果；所有运行落库，可在「运行记录」里重开。 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, apiJson } from './lib/api';

// ----------------------------------------------------------------------------- types

export type LabTool = 'overview' | 'screening' | 'committee' | 'backtest' | 'event-study' | 'scoreboard' | 'runs';
export const labMenu: { id: LabTool; label: string; icon: string }[] = [
  { id: 'overview', label: '概览', icon: '⌘' },
  { id: 'screening', label: '股票筛选', icon: '⌕' },
  { id: 'committee', label: '投资人委员会', icon: '⚖' },
  { id: 'backtest', label: '策略回测', icon: '↗' },
  { id: 'event-study', label: '事件研究', icon: '∿' },
  { id: 'scoreboard', label: '观察记分板', icon: '◎' },
  { id: 'runs', label: '运行记录', icon: '◷' },
];

type Universe = 'custom' | 'tech30' | 'sp500' | 'nasdaq100' | 'dow30' | 'holdings' | 'watchlist' | 'holdings_watchlist';
const INDEX_UNIVERSES: Universe[] = ['sp500', 'nasdaq100', 'dow30'];
const UNIVERSES: { id: Universe; label: string }[] = [
  { id: 'holdings', label: '当前持仓' }, { id: 'watchlist', label: 'Watchlist' }, { id: 'holdings_watchlist', label: '持仓 + Watchlist' }, { id: 'tech30', label: 'TECH_30 监控池' }, { id: 'dow30', label: '道琼斯 30' }, { id: 'nasdaq100', label: '纳斯达克 100' }, { id: 'sp500', label: '标普 500' }, { id: 'custom', label: '自定义' },
];
const UNIVERSE_LABEL: Record<string, string> = Object.fromEntries(UNIVERSES.map(u => [u.id, u.label]));

type ScreenCandidate = { ticker: string; price: number; price_change: number | null; market_cap: number | null; revenue_growth: number | null; gross_margin: number | null; volatility: number | null; high_52w: number | null; return_1w?: number | null; revenue_actual?: number | null; revenue_estimate?: number | null; [key: string]: unknown };
type ScreenRule = { field: string; op: 'gte' | 'lte'; value: number };
type CriterionMeta = { label: string; unit: 'pct' | 'usd' | 'x'; source: 'metrics' | 'prices' };
type CriteriaResp = { items: Record<string, CriterionMeta>; defaults: ScreenRule[] };
type ScreeningJob = { job_id: string; status: 'running' | 'completed' | 'failed'; done: number; total: number; universe: string; error?: string; result?: ScreeningResult };
type UniverseInfo = { size: number; as_of: string | null; label: string };
type Pricing = { prices_usd: Record<string, number>; committee_per_ticker: { full: number; lean: number } };
type ScreeningResult = { kind: 'screening'; lab_run_id?: string; universe: string; universe_as_of?: string | null; skipped?: Record<string, string>; data_source?: 'yfinance' | 'fd'; with_earnings?: boolean; fd_requests?: Record<string, number>; fd_cost_usd?: number; tickers: string[]; rules?: ScreenRule[]; rules_text?: string[]; rejected_count?: number; no_data?: string[]; reject_reasons?: Record<string, number>; date: string; universe_size: number; candidates: ScreenCandidate[] };

type CommitteeSource = 'tickers' | 'holdings' | 'watchlist' | 'screening';
type PersonaMeta = { key: string; name: string; name_zh: string; style: string; period: string; lookback: number; needs: string[] };
type CommitteePart = { name: string; score: number; max_score: number; details: string };
type CommitteeSignal = { persona: string; ticker: string; as_of: string; signal: 'bullish' | 'bearish' | 'neutral'; confidence: number; score: number; max_score: number; parts: CommitteePart[]; facts: Record<string, unknown>; margin_of_safety: number | null; reasoning: string; narrative?: string | null; narrative_grounded?: boolean | null; abstained: boolean; data_gaps: string[] };
type CommitteeVerdict = { ticker: string; stance: 'bullish' | 'bearish' | 'neutral' | 'abstain'; consensus: number; bullish: number; bearish: number; neutral: number; abstained: number; voters: number; agreement: number; avg_confidence: number; rank: number | null; signals: CommitteeSignal[]; position?: { weight: number | null; market_value: number; current_price: number | null; unrealized_pl_pct: number | null }; action?: string; action_reason?: string; price?: number | null };
type CommitteeResult = { kind: 'committee'; run_id: string; lab_run_id?: string; source: CommitteeSource; as_of: string; personas: string[]; personas_meta: PersonaMeta[]; elapsed_s: number; errors: Record<string, string>; cache_hits: string[]; data_gaps?: { gap: string; tickers: string[] }[]; lean?: boolean; fd_requests?: Record<string, number>; fd_cost_usd?: number; verdicts: CommitteeVerdict[]; top: { rank: number; ticker: string; stance: string; consensus: number }[]; screening?: { universe_size: number | null; n_candidates: number } };

type Trade = { ticker: string; direction: string; entry_date: string; exit_date: string; entry_price: number; exit_price: number; pnl: number; return_pct: number; holding_days: number };
type BacktestResult = { kind: 'backtest'; lab_run_id?: string; strategy: string; universe: string; tickers: string[]; params: Record<string, number>; trades: Trade[]; metrics: { total_return_pct: number; annualized_return_pct: number; sharpe_ratio: number; max_drawdown_pct: number; win_rate: number; n_trades: number; n_long: number; n_short: number; avg_return_pct: number; avg_holding_days: number } | null; equity_curve: number[] };

type WindowStats = { window: string; n_events: number; mean_car: number; std_car: number; t_stat: number; p_value: number; ci: { lower: number; upper: number; confidence: number } };
type EventCAR = { ticker: string; event_date: string; source_type: string; eps_surprise: string | null; car_0_1: number | null; car_0_5: number | null; car_0_20: number | null; market_model: { alpha: number; beta: number; r_squared: number } };
type EventStudyResult = { kind: 'event_study'; lab_run_id?: string; universe: string; tickers: string[]; params: Record<string, unknown>; events: EventCAR[]; aggregates: { source_type: string; n_events: number; windows: WindowStats[] }[]; skipped_tickers: string[] };

type ScoreboardRow = { persona: string; name_zh?: string; n: number; hits: number; hit_rate: number | null; avg_directional_1m: number | null };
type Scoreboard = { items: ScoreboardRow[]; counts: { runs: number; tickers: number; votes: number; scored_1m: number; scored_3m: number; due_1m: number; due_3m: number } };
type RunSummary = { id: string; kind: string; ran_at: string; tickers?: string[]; [key: string]: unknown };
type WatchlistItem = { ticker: string; added_at: string; note: string };

type ToolResult = ScreeningResult | CommitteeResult | BacktestResult | EventStudyResult;
type Handoff = { tickers: string[]; from: string };

// --------------------------------------------------------------------------- helpers

const pct = (v: number | null | undefined, d = 1) => v == null || !Number.isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(d)}%`;
const pctAbs = (v: number | null | undefined, d = 0) => v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(d)}%`;
const num = (v: number | null | undefined, d = 2) => v == null || !Number.isFinite(v) ? '—' : v.toFixed(d);
const usd = (v: number | null | undefined) => v == null || !Number.isFinite(v) ? '—' : `$${v.toFixed(2)}`;
const money = (v: number | null | undefined) => v == null || !Number.isFinite(v) ? '—' : Math.abs(v) >= 1e12 ? `$${(v / 1e12).toFixed(2)}T` : Math.abs(v) >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(0)}M` : `$${v.toFixed(2)}`;
const when = (iso: string) => { try { return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(iso)) } catch { return iso.slice(5, 16) } };
const parseTickers = (text: string) => Array.from(new Set(text.split(/[\s,，;]+/).map(t => t.trim().toUpperCase()).filter(Boolean)));
const errorText = (error: unknown) => error instanceof ApiError ? (error.detail || `HTTP ${error.status}`) : error instanceof Error ? error.message : String(error);
const KIND_LABEL: Record<string, string> = { screening: '股票筛选', committee: '投资人委员会', backtest: '策略回测', event_study: '事件研究', backfill: '收益回填' };
const SIGNAL_GLYPH: Record<string, string> = { bullish: '▲', bearish: '▼', neutral: '·', abstain: '—' };
const SIGNAL_LABEL: Record<string, string> = { bullish: '看多', bearish: '看空', neutral: '中性', abstain: '弃权' };
const STANCE_LABEL: Record<string, string> = { bullish: '偏多', bearish: '偏空', neutral: '中性', abstain: '无票' };

function useLabData<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null); const [error, setError] = useState('');
  const reload = useCallback(() => { if (!path) return; apiJson<T>(path).then(d => { setData(d); setError('') }).catch(e => setError(errorText(e))) }, [path]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { const t = window.setTimeout(reload, 0); return () => window.clearTimeout(t) }, [reload, ...deps]);
  return { data, error, reload };
}

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) { return <label className="lab-field"><span>{label}</span>{children}{hint ? <small>{hint}</small> : null}</label> }
function NumberInput({ value, onChange, min, max, step }: { value: string; onChange: (v: string) => void; min?: number; max?: number; step?: number }) { return <input type="number" value={value} min={min} max={max} step={step} onChange={e => onChange(e.target.value)}/> }
function Chips<T extends string>({ options, value, onChange }: { options: { id: T; label: string }[]; value: T; onChange: (v: T) => void }) { return <div className="lab-chips">{options.map(o => <button key={o.id} type="button" className={o.id === value ? 'active' : ''} onClick={() => onChange(o.id)}>{o.label}</button>)}</div> }
function UniversePicker({ universe, setUniverse, tickers, setTickers, exclude = [], info }: { universe: Universe; setUniverse: (u: Universe) => void; tickers: string; setTickers: (t: string) => void; exclude?: Universe[]; info?: Record<string, UniverseInfo> | null }) {
  const meta = info?.[universe];
  return <><Field label="股票池" hint={meta ? `${meta.size} 只${meta.as_of ? ` · 成分股快照 ${meta.as_of}` : ''}${INDEX_UNIVERSES.includes(universe) ? ' · 超过 40 只会转为后台任务，可离开页面' : ''}` : undefined}><Chips options={UNIVERSES.filter(u => !exclude.includes(u.id))} value={universe} onChange={setUniverse}/></Field>{universe === 'custom' && <Field label="股票代码" hint="逗号或空格分隔，最多 60 只"><input value={tickers} onChange={e => setTickers(e.target.value.toUpperCase())} placeholder="AAPL, MSFT, NVDA"/></Field>}</>;
}
function Empty({ glyph, title, text }: { glyph: string; title: string; text: string }) { return <div className="lab-empty"><span>{glyph}</span><h3>{title}</h3><p>{text}</p></div> }
function ErrorBox({ text }: { text: string }) { return <div className="lab-error"><strong>请求失败</strong><span>{text}</span></div> }
function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) { return <div className="lab-stat"><span>{label}</span><strong className={tone || ''}>{value}</strong></div> }
function RawJson({ data }: { data: unknown }) { return <details className="lab-raw"><summary>原始结果</summary><pre>{JSON.stringify(data, null, 2)}</pre></details> }
function AddToWatchlist({ ticker, watchlist, onAdded }: { ticker: string; watchlist: Set<string>; onAdded: () => void }) {
  const [busy, setBusy] = useState(false);
  if (watchlist.has(ticker)) return <em className="lab-tag-ok">已在 Watchlist</em>;
  return <button type="button" className="lab-link" disabled={busy} onClick={async () => { setBusy(true); try { await apiJson('/api/watchlist', { method: 'POST', body: JSON.stringify({ ticker, note: '来自实验室' }) }); onAdded() } finally { setBusy(false) } }}>{busy ? '…' : '+ Watchlist'}</button>;
}

// ------------------------------------------------------------------------------ page

type Ask = (prompt: string, context: string) => void;
type ToolProps = { ask: Ask; selectTool: (t: LabTool) => void; watchlist: Set<string>; refreshWatchlist: () => void };

export function LabPage({ tool, selectTool, ask }: { tool: LabTool; selectTool: (t: LabTool) => void; ask: Ask; askNow?: Ask }) {
  const [results, setResults] = useState<Partial<Record<string, ToolResult>>>({});
  const [handoff, setHandoff] = useState<Handoff | null>(null);
  const wl = useLabData<{ items: WatchlistItem[] }>('/api/watchlist');
  const watchlist = useMemo(() => new Set((wl.data?.items || []).map(i => i.ticker)), [wl.data]);
  const setResult = useCallback((kind: string, r: ToolResult | undefined) => setResults(current => ({ ...current, [kind]: r })), []);
  const hand = useCallback((tickers: string[], from: string, to: LabTool) => { setHandoff({ tickers, from }); selectTool(to) }, [selectTool]);
  const common: ToolProps = { ask, selectTool, watchlist, refreshWatchlist: wl.reload };
  const heading: Record<LabTool, [string, string]> = {
    overview: ['实验室', '筛选 → 委员会 → 回测 → 观察 → 批准。每一步都是确定性的引擎，结果全部落库；实验室不写任何生产状态。'],
    screening: ['股票筛选', '基本面硬规则过滤股票池，得到候选名单，可直接送入委员会。'],
    committee: ['投资人委员会', '13 位模拟投资人各按一份确定性清单打分，按置信度加权投票；LLM 只在你点「解读」时写文字。'],
    backtest: ['策略回测', '在历史财报事件上模拟交易，给出收益、回撤和胜率。'],
    'event-study': ['事件研究', '财报公布后的累计异常收益（CAR）及其显著性。'],
    scoreboard: ['观察记分板', '委员会每一票在 1 个月 / 3 个月后对不对：无前视的逐人命中率。'],
    runs: ['运行记录', '所有工具的历史运行，点开可原样重看。'],
  };
  return <div className="page lab-page">
    <div className="page-heading"><div><h1>{heading[tool][0]}</h1><p>{heading[tool][1]}</p></div><span className="lab-tag">ISOLATED LAB</span></div>
    {tool === 'overview' && <OverviewTool {...common}/>}
    {tool === 'screening' && <ScreeningTool {...common} result={results.screening as ScreeningResult | undefined} setResult={r => setResult('screening', r)} onHand={hand}/>}
    {tool === 'committee' && <CommitteeTool {...common} result={results.committee as CommitteeResult | undefined} setResult={r => setResult('committee', r)} handoff={handoff} clearHandoff={() => setHandoff(null)} onHand={hand}/>}
    {tool === 'backtest' && <BacktestTool {...common} result={results.backtest as BacktestResult | undefined} setResult={r => setResult('backtest', r)} handoff={handoff} clearHandoff={() => setHandoff(null)}/>}
    {tool === 'event-study' && <EventStudyTool {...common} result={results.event_study as EventStudyResult | undefined} setResult={r => setResult('event_study', r)} handoff={handoff} clearHandoff={() => setHandoff(null)}/>}
    {tool === 'scoreboard' && <ScoreboardTool {...common}/>}
    {tool === 'runs' && <RunsTool {...common} onOpen={(kind, result) => { setResult(kind === 'event_study' ? 'event_study' : kind, result as ToolResult); selectTool(kind === 'event_study' ? 'event-study' : kind as LabTool) }}/>}
  </div>;
}

function HandoffBanner({ handoff, onUse, onClear }: { handoff: Handoff; onUse: () => void; onClear: () => void }) {
  return <div className="lab-handoff"><span>来自「{handoff.from}」的 {handoff.tickers.length} 只：{handoff.tickers.slice(0, 12).join(' ')}{handoff.tickers.length > 12 ? ' …' : ''}</span><button type="button" onClick={onUse}>用作股票池</button><button type="button" className="lab-link" onClick={onClear}>忽略</button></div>;
}

// -------------------------------------------------------------------------- overview

function OverviewTool({ selectTool, watchlist, ask }: ToolProps) {
  const runs = useLabData<{ items: RunSummary[]; counts: Record<string, number> }>('/api/lab/runs?limit=8');
  const board = useLabData<Scoreboard>('/api/lab/committee/scoreboard');
  const latest = (kind: string) => runs.data?.items.find(r => r.kind === kind);
  const scr = latest('screening'); const com = latest('committee'); const bt = latest('backtest'); const counts = board.data?.counts;
  const steps: { id: LabTool; title: string; line: string; count: string }[] = [
    { id: 'screening', title: '① 筛选', line: scr ? `${when(scr.ran_at)} · ${UNIVERSE_LABEL[String(scr.universe)] || scr.universe} ${scr.universe_size} → ${scr.n_candidates} 只候选` : '还没跑过', count: String(runs.data?.counts.screening || 0) },
    { id: 'committee', title: '② 委员会', line: com ? `${when(com.ran_at)} · ${com.n_tickers} 只 · 偏多 ${(com.stances as Record<string, number>)?.bullish ?? 0} / 偏空 ${(com.stances as Record<string, number>)?.bearish ?? 0}` : '还没跑过', count: String(runs.data?.counts.committee || 0) },
    { id: 'backtest', title: '③ 回测', line: bt ? `${when(bt.ran_at)} · ${bt.n_trades} 笔 · 总收益 ${pct(bt.total_return_pct as number)} · 夏普 ${num(bt.sharpe_ratio as number)}` : '还没跑过', count: String(runs.data?.counts.backtest || 0) },
    { id: 'scoreboard', title: '④ 观察', line: counts ? `${counts.votes} 票 · 已评分 ${counts.scored_1m} / 待评分 ${counts.due_1m}（1 月）` : '…', count: String(counts?.scored_1m ?? 0) },
    { id: 'runs', title: '⑤ 批准', line: `Watchlist ${watchlist.size} 只 · 生产阈值只读`, count: String(watchlist.size) },
  ];
  return <>
    <section className="surface lab-pipeline">{steps.map(s => <button key={s.id} type="button" onClick={() => selectTool(s.id)}><strong>{s.title}</strong><em>{s.count}</em><span>{s.line}</span></button>)}</section>
    <div className="lab-two">
      <section className="surface"><div className="surface-header"><div><h2>最近运行</h2><span>所有工具 · 落库</span></div><button type="button" onClick={runs.reload}>刷新</button></div>
        {runs.error ? <ErrorBox text={runs.error}/> : !runs.data?.items.length ? <Empty glyph="◷" title="还没有运行记录" text="从「股票筛选」或「投资人委员会」开始。"/> : <div className="lab-runs">{runs.data.items.map(r => <RunRow key={r.id} run={r} onOpen={() => selectTool('runs')}/>)}</div>}
      </section>
      <section className="surface"><div className="surface-header"><div><h2>引擎</h2><span>全部确定性，无 LLM 判决</span></div></div>
        <div className="lab-engines">
          {[['股票筛选', '市值 / 营收增长 / 毛利率 / 波动率硬规则', 'screening'], ['投资人委员会', '13 套打分清单 · 置信度加权投票', 'committee'], ['PEAD 回测', '财报后漂移策略 · 逐笔交易', 'backtest'], ['事件研究', '市场模型 + bootstrap 置信区间', 'event-study'], ['前向记分', '每天 02:30 ET 回填真实收益', 'scoreboard']].map(([n, d, t]) => <button key={n} type="button" onClick={() => selectTool(t as LabTool)}><strong>{n}</strong><span>{d}</span></button>)}
        </div>
        <div className="guardrail"><strong>生产保护</strong><span>实验室不创建告警、不下单、不改监控阈值；唯一的「批准」动作是把股票加进 Watchlist。</span></div>
      </section>
    </div>
    <button type="button" className="explain-button lab-ask" onClick={() => ask('解释实验室五步流程（筛选、委员会、回测、观察、批准）各自解决什么问题', '实验室 · 概览')}>问 AI：这套流程怎么用</button>
  </>;
}

function RunRow({ run, onOpen }: { run: RunSummary; onOpen: () => void }) {
  const costTag = typeof run.fd_cost_usd === 'number' ? ` · FD ${usd(run.fd_cost_usd)}` : '';
  const line = run.kind === 'screening' ? `${UNIVERSE_LABEL[String(run.universe)] || run.universe} ${run.universe_size} → ${run.n_candidates} 只${costTag}`
    : run.kind === 'committee' ? `${run.n_tickers} 只 · 偏多 ${(run.stances as Record<string, number>)?.bullish ?? 0} 偏空 ${(run.stances as Record<string, number>)?.bearish ?? 0}${(run.top as string[])?.length ? ` · 榜首 ${(run.top as string[])[0]}` : ''}${costTag}`
    : run.kind === 'backtest' ? `${run.n_trades} 笔 · ${pct(run.total_return_pct as number)} · 夏普 ${num(run.sharpe_ratio as number)}`
    : run.kind === 'event_study' ? `${run.n_events} 个事件 · ${run.n_groups} 组`
    : run.kind === 'backfill' ? `回填 ${run.filled} / ${run.checked}` : '';
  return <button type="button" className="lab-run" onClick={onOpen}><em className={`kind-${run.kind}`}>{KIND_LABEL[run.kind] || run.kind}</em><span>{line}</span><time>{when(run.ran_at)}</time></button>;
}

// ------------------------------------------------------------------------- screening

/** Order, default operator and default value for each criterion; labels/units come from the backend catalog. */
const CRITERIA_UI: { field: string; op: 'gte' | 'lte'; def: string; hint?: string }[] = [
  { field: 'market_cap', op: 'gte', def: '10' }, { field: 'price', op: 'gte', def: '5' },
  { field: 'revenue_growth', op: 'gte', def: '5' }, { field: 'earnings_growth', op: 'gte', def: '10' },
  { field: 'gross_margin', op: 'gte', def: '50' }, { field: 'operating_margin', op: 'gte', def: '15' }, { field: 'net_margin', op: 'gte', def: '10' },
  { field: 'return_on_equity', op: 'gte', def: '15' }, { field: 'return_on_invested_capital', op: 'gte', def: '10' },
  { field: 'debt_to_equity', op: 'lte', def: '1' }, { field: 'current_ratio', op: 'gte', def: '1' },
  { field: 'price_to_earnings_ratio', op: 'lte', def: '30' }, { field: 'price_to_sales_ratio', op: 'lte', def: '10' }, { field: 'price_to_book_ratio', op: 'lte', def: '10' },
  { field: 'free_cash_flow_yield', op: 'gte', def: '3' }, { field: 'payout_ratio', op: 'lte', def: '60' },
  { field: 'volatility', op: 'lte', def: '60' }, { field: 'return_1w', op: 'gte', def: '0' }, { field: 'return_1m', op: 'gte', def: '0' }, { field: 'return_3m', op: 'gte', def: '0' },
  { field: 'pct_from_52w_high', op: 'gte', def: '-15' }, { field: 'pct_from_52w_low', op: 'gte', def: '20' },
];
const DEFAULT_ENABLED = ['market_cap', 'revenue_growth', 'gross_margin', 'volatility'];
const unitLabel = (unit: string, field: string) => unit === 'pct' ? '%' : unit === 'usd' ? (field === 'market_cap' ? '十亿美元' : '美元') : '倍';
const toBackend = (field: string, unit: string, raw: string) => { const n = Number(raw); return unit === 'pct' ? n / 100 : unit === 'usd' && field === 'market_cap' ? n * 1e9 : n };
const fmtCell = (unit: string, field: string, v: unknown) => typeof v !== 'number' ? '—' : unit === 'pct' ? pct(v) : unit === 'usd' ? (field === 'market_cap' ? money(v) : `$${num(v)}`) : num(v);


function ScreeningTool({ result, setResult, onHand, watchlist, refreshWatchlist, ask }: ToolProps & { result?: ScreeningResult; setResult: (r?: ScreeningResult) => void; onHand: (t: string[], from: string, to: LabTool) => void }) {
  const [universe, setUniverse] = useState<Universe>('tech30'); const [tickers, setTickers] = useState('');
  const criteria = useLabData<CriteriaResp>('/api/lab/screening/criteria');
  const [enabled, setEnabled] = useState<Set<string>>(new Set(DEFAULT_ENABLED));
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(CRITERIA_UI.map(c => [c.field, c.def])));
  const [ops, setOps] = useState<Record<string, 'gte' | 'lte'>>(() => Object.fromEntries(CRITERIA_UI.map(c => [c.field, c.op])));
  const activeRules = (): ScreenRule[] => CRITERIA_UI.filter(c => enabled.has(c.field) && values[c.field] !== '' && Number.isFinite(Number(values[c.field]))).map(c => ({ field: c.field, op: ops[c.field], value: toBackend(c.field, criteria.data?.items[c.field]?.unit || 'x', values[c.field]) }));
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [picked, setPicked] = useState<Set<string>>(new Set()); const [job, setJob] = useState<ScreeningJob | null>(null);
  const [dataSource, setDataSource] = useState<'yfinance' | 'fd'>('yfinance'); const [withEarnings, setWithEarnings] = useState(false);
  const info = useLabData<{ items: Record<string, UniverseInfo> }>('/api/lab/universes');
  const pricing = useLabData<Pricing>('/api/lab/committee/pricing');
  const poolSize = universe === 'custom' ? parseTickers(tickers).length : (info.data?.items[universe]?.size ?? 0);
  const estMetrics = dataSource === 'fd' ? poolSize * (pricing.data?.prices_usd.financial_metrics ?? 0.02) : 0;
  const run = async () => {
    setBusy(true); setError(''); setJob(null);
    try {
      const body = { universe, tickers: universe === 'custom' ? parseTickers(tickers) : [], data_source: dataSource, with_earnings: dataSource === 'fd' && withEarnings, rules: activeRules() };
      let r = await apiJson<ScreeningResult | ScreeningJob>('/api/lab/screening', { method: 'POST', body: JSON.stringify(body) });
      while ('job_id' in r) {
        setJob(r);
        if (r.status === 'failed') throw new Error(r.error || '筛选任务失败');
        if (r.status === 'completed' && r.result) { r = r.result; break }
        await new Promise<void>(resolve => window.setTimeout(resolve, 2000));
        r = await apiJson<ScreeningJob>(`/api/lab/screening/jobs/${encodeURIComponent(r.job_id)}`);
      }
      const done = r as ScreeningResult; setResult(done); setPicked(new Set(done.candidates.map(c => c.ticker)));
    } catch (e) { setError(errorText(e)) } finally { setBusy(false); setJob(null) }
  };
  const chosen = result ? result.candidates.filter(c => picked.has(c.ticker)).map(c => c.ticker) : [];
  return <div className="lab-tool">
    <section className="surface lab-config"><div className="surface-header"><div><h2>筛选条件</h2><span>全部阈值可改，缺数据的股票不通过</span></div></div>
      <UniversePicker universe={universe} setUniverse={setUniverse} tickers={tickers} setTickers={setTickers} info={info.data?.items}/>
      <Field label="数据源" hint={dataSource === 'yfinance' ? '市值、营收增长、毛利率来自 yfinance，免费；口径：营收增长为最近一季同比，毛利率为 TTM' : `Financial Datasets 按请求计费，约 ${usd(pricing.data?.prices_usd.financial_metrics ?? 0.02)}/只`}><Chips options={[{ id: 'yfinance', label: 'yfinance（免费）' }, { id: 'fd', label: 'Financial Datasets（付费）' }]} value={dataSource} onChange={setDataSource}/></Field>
      {dataSource === 'fd' && <Field label="候选的华尔街财报预期" hint={`每只候选约 ${usd(pricing.data?.prices_usd.earnings ?? 0.02)}`}><Chips options={[{ id: 'no', label: '不取' }, { id: 'yes', label: '取' }]} value={withEarnings ? 'yes' : 'no'} onChange={v => setWithEarnings(v === 'yes')}/></Field>}
      <Field label={`筛选条件（已启用 ${enabled.size}）`} hint="勾选的条件全部满足才通过；某只股票缺该字段即不通过该条。yfinance 可能缺少部分财务比率，缺失的会显示为 —。"><div className="lab-criteria">{CRITERIA_UI.map(c => { const meta = criteria.data?.items[c.field]; const on = enabled.has(c.field); const unit = meta?.unit || 'x'; return <div key={c.field} className={on ? 'on' : ''}><input type="checkbox" checked={on} onChange={() => setEnabled(cur => { const next = new Set(cur); if (next.has(c.field)) next.delete(c.field); else next.add(c.field); return next })}/><span className="lab-crit-label">{meta?.label || c.field}</span><button type="button" className="lab-op" disabled={!on} onClick={() => setOps(cur => ({ ...cur, [c.field]: cur[c.field] === 'gte' ? 'lte' : 'gte' }))}>{ops[c.field] === 'gte' ? '≥' : '≤'}</button><input type="number" disabled={!on} value={values[c.field]} onChange={e => setValues(cur => ({ ...cur, [c.field]: e.target.value }))}/><small>{unitLabel(unit, c.field)}</small></div> })}</div></Field>
      <p className="lab-note">预计 Financial Datasets 费用：{estMetrics > 0 ? `≈ ${usd(estMetrics)}（${poolSize} 次指标请求）` : '$0.00'}{withEarnings ? ` + 每只候选 ${usd(pricing.data?.prices_usd.earnings ?? 0.02)}` : ''}。价格来自 /api/lab/committee/pricing，可用 FD_PRICES 环境变量校正。</p>
      <button className="run-button" disabled={busy || !enabled.size || (universe === 'custom' && !parseTickers(tickers).length)} onClick={() => void run()}>{busy ? (job ? `筛选中… ${job.done} / ${job.total}` : '筛选中…') : '运行筛选'}</button>
      {job && <div className="lab-progress"><i style={{ width: `${job.total ? Math.round((job.done / job.total) * 100) : 0}%` }}/></div>}
    </section>
    <section className="surface lab-result lab-result-clamp">
      {error ? <ErrorBox text={error}/> : !result ? <Empty glyph="⌕" title="等待筛选" text="选一个股票池、勾选条件，结果是全部通过的候选名单。可以整单送进委员会。"/> : <>
        <div className="surface-header"><div><h2>{result.candidates.length} / {result.universe_size} 只通过</h2><span>{UNIVERSE_LABEL[result.universe] || result.universe}{result.universe_as_of ? `（成分股 ${result.universe_as_of}）` : ''} · {result.date} · {result.data_source === 'fd' ? 'Financial Datasets' : 'yfinance'} · FD 费用 {usd(result.fd_cost_usd ?? 0)}{(result.no_data?.length || 0) + Object.keys(result.skipped || {}).length ? ` · 无数据跳过 ${new Set([...(result.no_data || []), ...Object.keys(result.skipped || {})]).size} 只` : ''} · 条件：{(result.rules_text || []).join('，') || '无'}</span></div>
          <div className="lab-actions"><button type="button" disabled={!chosen.length} onClick={() => onHand(chosen, '股票筛选', 'committee')}>送入委员会（{chosen.length}）</button><button type="button" disabled={!chosen.length} onClick={() => onHand(chosen, '股票筛选', 'backtest')}>送入回测</button></div></div>
        {result.candidates.length === 0 ? <div className="lab-note">没有股票通过。{result.reject_reasons && Object.keys(result.reject_reasons).length ? ` 最常见的不通过原因：${Object.entries(result.reject_reasons).slice(0, 3).map(([r, n]) => `${r}（${n} 只）`).join('，')}。` : ''}放宽条件，或换一个股票池。</div> : (() => { const core = new Set(['market_cap', 'revenue_growth', 'gross_margin', 'volatility']); const extra = (result.rules || []).map(r => r.field).filter((f, i, a) => !core.has(f) && f !== 'price' && a.indexOf(f) === i); return <div className="lab-table-wrap lab-scroll"><table className="lab-table"><thead><tr><th><input type="checkbox" checked={picked.size === result.candidates.length} onChange={e => setPicked(e.target.checked ? new Set(result.candidates.map(c => c.ticker)) : new Set())}/></th><th>股票</th><th>价格</th><th>1 日</th><th>1 周</th><th>市值</th><th>营收增长</th><th>毛利率</th><th>波动率</th>{extra.map(f => <th key={f}>{criteria.data?.items[f]?.label || f}</th>)}<th></th></tr></thead><tbody>
          {result.candidates.map(c => <tr key={c.ticker}><td><input type="checkbox" checked={picked.has(c.ticker)} onChange={() => setPicked(cur => { const next = new Set(cur); if (next.has(c.ticker)) next.delete(c.ticker); else next.add(c.ticker); return next })}/></td><td><strong>{c.ticker}</strong></td><td>${num(c.price)}</td><td className={(c.price_change || 0) >= 0 ? 'positive' : 'negative'}>{pct(c.price_change)}</td><td className={(c.return_1w || 0) >= 0 ? 'positive' : 'negative'}>{pct(c.return_1w)}</td><td>{money(c.market_cap)}</td><td>{pct(c.revenue_growth)}</td><td>{pctAbs(c.gross_margin)}</td><td>{pctAbs(c.volatility)}</td>{extra.map(f => <td key={f}>{fmtCell(criteria.data?.items[f]?.unit || 'x', f, c[f])}</td>)}<td><AddToWatchlist ticker={c.ticker} watchlist={watchlist} onAdded={refreshWatchlist}/></td></tr>)}
        </tbody></table></div> })()}
        <div className="lab-foot"><button type="button" className="explain-button" onClick={() => ask(`股票筛选结果：${result.candidates.slice(0, 40).map(c => c.ticker).join(', ') || '无'}${result.candidates.length > 40 ? ` 等 ${result.candidates.length} 只` : ''}（股票池 ${result.universe_size} 只，条件：${(result.rules_text || []).join('，')}）。请点评这批候选的共同点和明显遗漏。`, '实验室 · 股票筛选')}>问 AI 点评这批候选</button><RawJson data={result}/></div>
      </>}
    </section>
  </div>;
}

// ------------------------------------------------------------------------- committee

const COMMITTEE_SOURCES: { id: CommitteeSource; label: string; hint: string }[] = [
  { id: 'holdings', label: '当前持仓', hint: '审视每只持仓：增持 / 持有 / 减持候选' },
  { id: 'watchlist', label: 'Watchlist', hint: '观察名单全部排名' },
  { id: 'tickers', label: '自定义', hint: '逗号分隔，最多 60 只，全部排名' },
  { id: 'screening', label: '先筛选再评审', hint: '用默认阈值跑一遍股票筛选，再对候选评审并选出前 N 只' },
];
const FALLBACK_PERSONAS: PersonaMeta[] = [['warren_buffett','Warren Buffett','沃伦·巴菲特'],['charlie_munger','Charlie Munger','查理·芒格'],['ben_graham','Ben Graham','本杰明·格雷厄姆'],['peter_lynch','Peter Lynch','彼得·林奇'],['phil_fisher','Phil Fisher','菲利普·费雪'],['bill_ackman','Bill Ackman','比尔·阿克曼'],['cathie_wood','Cathie Wood','凯茜·伍德'],['michael_burry','Michael Burry','迈克尔·伯里'],['mohnish_pabrai','Mohnish Pabrai','莫尼什·帕伯莱'],['stanley_druckenmiller','Stanley Druckenmiller','斯坦利·德鲁肯米勒'],['aswath_damodaran','Aswath Damodaran','阿斯瓦斯·达摩达兰'],['nassim_taleb','Nassim Taleb','纳西姆·塔勒布'],['rakesh_jhunjhunwala','Rakesh Jhunjhunwala','拉克什·金君瓦拉']].map(([key, name, name_zh]) => ({ key, name, name_zh, style: '', period: '', lookback: 0, needs: [] }));

function CommitteeTool({ result, setResult, handoff, clearHandoff, onHand, watchlist, refreshWatchlist, ask }: ToolProps & { result?: CommitteeResult; setResult: (r?: CommitteeResult) => void; handoff: Handoff | null; clearHandoff: () => void; onHand: (t: string[], from: string, to: LabTool) => void }) {
  const [source, setSource] = useState<CommitteeSource>('holdings'); const [tickers, setTickers] = useState('AAPL, MSFT, NVDA');
  const [topN, setTopN] = useState('15'); const [maxWeight, setMaxWeight] = useState('15'); const [useCache, setUseCache] = useState(true); const [lean, setLean] = useState(true);
  const pricing = useLabData<Pricing>('/api/lab/committee/pricing');
  const perTicker = lean ? pricing.data?.committee_per_ticker.lean : pricing.data?.committee_per_ticker.full;
  const knownCount = source === 'tickers' ? parseTickers(tickers).length : source === 'screening' ? Number(topN) || 15 : null;
  const personasData = useLabData<{ items: PersonaMeta[] }>('/api/lab/committee/personas');
  const personas = personasData.data?.items?.length ? personasData.data.items : FALLBACK_PERSONAS;
  const [selected, setSelected] = useState<string[]>(FALLBACK_PERSONAS.map(p => p.key));
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const history = useLabData<{ items: { run_id: string; created_at: string; source: string; n_tickers: number; tickers: string[] }[] }>('/api/lab/committee/runs?limit=10', [result?.run_id]);
  const allSelected = selected.length === personas.length;
  const run = async () => {
    setBusy(true); setError('');
    try {
      const body: Record<string, unknown> = { source, personas: allSelected ? undefined : selected, use_cache: useCache, lean, max_weight: (Number(maxWeight) || 15) / 100, ...(source === 'tickers' ? { tickers: parseTickers(tickers) } : {}), ...(source === 'screening' ? { top_n: Number(topN) || 15 } : {}) };
      setResult(await apiJson<CommitteeResult>('/api/lab/committee', { method: 'POST', body: JSON.stringify(body) }));
    } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  };
  const reopen = async (runId: string) => { setBusy(true); setError(''); try { setResult(await apiJson<CommitteeResult>(`/api/lab/committee/runs/${encodeURIComponent(runId)}`)) } catch (e) { setError(errorText(e)) } finally { setBusy(false) } };
  return <>
    {handoff && <HandoffBanner handoff={handoff} onUse={() => { setSource('tickers'); setTickers(handoff.tickers.join(', ')); clearHandoff() }} onClear={clearHandoff}/>}
    <div className="lab-tool">
      <section className="surface lab-config"><div className="surface-header"><div><h2>评审设置</h2><span>每位投资人一份确定性打分清单 · 按置信度加权投票</span></div></div>
        <Field label="输入来源" hint={COMMITTEE_SOURCES.find(s => s.id === source)?.hint}><Chips options={COMMITTEE_SOURCES} value={source} onChange={setSource}/></Field>
        {source === 'tickers' && <Field label="股票代码"><input value={tickers} onChange={e => setTickers(e.target.value.toUpperCase())}/></Field>}
        {source === 'holdings' && <Field label="单只持仓权重上限（%）" hint="共识看多但权重已达上限时标为「持有」而不是「增持候选」"><NumberInput value={maxWeight} onChange={setMaxWeight} min={1} max={100}/></Field>}
        {source === 'screening' && <Field label="从候选中选出前 N 只"><NumberInput value={topN} onChange={setTopN} min={1} max={60}/></Field>}
        <Field label={`参与投票的投资人（${selected.length}/${personas.length}）`}><div className="persona-chips">{personas.map(p => <button key={p.key} type="button" className={selected.includes(p.key) ? 'active' : ''} title={p.style} onClick={() => setSelected(cur => cur.includes(p.key) ? cur.filter(k => k !== p.key) : [...cur, p.key])}>{p.name_zh || p.name}</button>)}<button type="button" className="lab-link" onClick={() => setSelected(allSelected ? [] : personas.map(p => p.key))}>{allSelected ? '全不选' : '全选'}</button></div></Field>
        <Field label="数据"><Chips options={[{ id: 'cache', label: '复用当日快照' }, { id: 'fresh', label: '重新取数' }]} value={useCache ? 'cache' : 'fresh'} onChange={v => setUseCache(v === 'cache')}/></Field>
        <Field label="省流模式" hint={lean ? '跳过新闻和内部人交易两路付费请求；只影响几位投资人的情绪小分项' : '取全部数据，每只多两次付费请求'}><Chips options={[{ id: 'lean', label: '开（省流）' }, { id: 'full', label: '关（全量）' }]} value={lean ? 'lean' : 'full'} onChange={v => setLean(v === 'lean')}/></Field>
        <p className="lab-note">预计 Financial Datasets 费用：每只约 {usd(perTicker)}{knownCount ? `，${knownCount} 只约 ${usd((perTicker ?? 0) * knownCount)}` : ''}；当日已有快照的股票不再计费。</p>
        <button className="run-button" disabled={busy || !selected.length || (source === 'tickers' && !parseTickers(tickers).length)} onClick={() => void run()}>{busy ? '评审中…（首次取数约 5 秒/只）' : '开始评审'}</button>
        {history.data?.items.length ? <div className="lab-history"><span>历史运行</span>{history.data.items.map(h => <button key={h.run_id} type="button" onClick={() => void reopen(h.run_id)} className={result?.run_id === h.run_id ? 'active' : ''}>{when(h.created_at)} · {COMMITTEE_SOURCES.find(s => s.id === h.source)?.label || h.source} · {h.n_tickers} 只</button>)}</div> : null}
      </section>
      <section className="surface lab-result">
        {error ? <ErrorBox text={error}/> : !result ? <Empty glyph="⚖" title="等待评审" text="结果是一张矩阵：行是投资人，列是股票，格子里是多空与置信度。点任意格子看依据。"/> : <CommitteeView result={result} personas={personas} watchlist={watchlist} refreshWatchlist={refreshWatchlist} ask={ask} onHand={onHand}/>}
      </section>
    </div>
  </>;
}

type NarrativeState = { text: string; grounded: boolean | null } | { error: string } | 'loading';

function CommitteeView({ result, personas, watchlist, refreshWatchlist, ask, onHand }: { result: CommitteeResult; personas: PersonaMeta[]; watchlist: Set<string>; refreshWatchlist: () => void; ask: Ask; onHand: (t: string[], from: string, to: LabTool) => void }) {
  const [picked, setPicked] = useState<{ ticker: string; persona: string } | null>(null);
  const [narratives, setNarratives] = useState<Record<string, NarrativeState>>({});
  const verdicts = useMemo(() => result.verdicts || [], [result.verdicts]);
  const meta = result.personas_meta?.length ? result.personas_meta : personas.filter(p => result.personas.includes(p.key));
  const grid = useMemo(() => { const m = new Map<string, CommitteeSignal>(); verdicts.forEach(v => v.signals.forEach(s => m.set(`${s.persona}|${v.ticker}`, s))); return m }, [verdicts]);
  const pickedSignal = picked ? grid.get(`${picked.persona}|${picked.ticker}`) : undefined; const pickedVerdict = picked ? verdicts.find(v => v.ticker === picked.ticker) : undefined;
  const isHoldings = result.source === 'holdings';
  const narrate = async (ticker: string, persona: string) => {
    const key = `${persona}|${ticker}`; setNarratives(c => ({ ...c, [key]: 'loading' }));
    try { const d = await apiJson<{ narrative: string; narrative_grounded: boolean | null }>('/api/lab/committee/narrate', { method: 'POST', body: JSON.stringify({ run_id: result.run_id, ticker, persona, language: 'zh' }) }); setNarratives(c => ({ ...c, [key]: { text: d.narrative, grounded: d.narrative_grounded } })) }
    catch (e) { setNarratives(c => ({ ...c, [key]: { error: errorText(e) } })) }
  };
  const bullishTickers = verdicts.filter(v => v.stance === 'bullish').map(v => v.ticker);
  return <>
    <div className="surface-header"><div><h2>{verdicts.length} 只 · {meta.length} 位投资人</h2><span>{COMMITTEE_SOURCES.find(s => s.id === result.source)?.label || result.source} · as of {result.as_of} · {num(result.elapsed_s, 1)}s · FD 费用 {usd(result.fd_cost_usd ?? 0)}{result.lean ? '（省流）' : ''}{result.cache_hits?.length ? ` · ${result.cache_hits.length} 只命中当日缓存` : ''}{result.screening ? ` · 初筛 ${result.screening.universe_size ?? '?'} → ${result.screening.n_candidates}` : ''}</span></div>
      <div className="lab-actions"><button type="button" disabled={!bullishTickers.length} onClick={() => onHand(bullishTickers, '委员会偏多', 'backtest')}>偏多的送入回测（{bullishTickers.length}）</button></div></div>
    {Object.keys(result.errors || {}).length > 0 && <div className="committee-errors">{Object.entries(result.errors).map(([t, e]) => <span key={t}><strong>{t}</strong> {e}</span>)}</div>}
    {result.data_gaps && result.data_gaps.length > 0 && <div className="lab-gaps"><strong>数据缺口</strong>{result.data_gaps.map(g => <div key={g.gap}><span>{g.gap}</span><small>{g.tickers.length} 只：{g.tickers.slice(0, 12).join(' ')}{g.tickers.length > 12 ? ' …' : ''}</small></div>)}<p>缺少所需输入的投资人会弃权而不是打低分；弃权票不参与共识。</p></div>}
    <div className="lab-verdicts">{verdicts.map(v => <div key={v.ticker} className={`lab-verdict stance-${v.stance}`}><strong>{v.ticker}</strong><em>{isHoldings ? (v.action || '—') : `#${v.rank} ${STANCE_LABEL[v.stance]}`}</em><span>共识 {v.consensus >= 0 ? '+' : ''}{v.consensus.toFixed(2)} · {v.bullish}▲ {v.bearish}▼ {v.neutral}· {v.abstained ? `${v.abstained}弃` : ''}</span>{isHoldings && v.position ? <span>权重 {pctAbs(v.position.weight, 1)}{v.position.unrealized_pl_pct != null ? ` · 浮盈 ${pct(v.position.unrealized_pl_pct)}` : ''}</span> : null}{isHoldings && v.action_reason ? <small>{v.action_reason}</small> : null}<AddToWatchlist ticker={v.ticker} watchlist={watchlist} onAdded={refreshWatchlist}/></div>)}</div>
    <div className="committee-matrix-wrap"><table className="committee-matrix"><thead><tr><th>投资人</th>{verdicts.map(v => <th key={v.ticker}>{v.ticker}</th>)}</tr></thead><tbody>
      {meta.map(p => <tr key={p.key}><th title={p.style}>{p.name_zh || p.name}</th>{verdicts.map(v => { const s = grid.get(`${p.key}|${v.ticker}`); const kind = !s || s.abstained ? 'abstain' : s.signal; const active = picked?.ticker === v.ticker && picked?.persona === p.key; return <td key={v.ticker}><button type="button" className={`sig sig-${kind} ${active ? 'active' : ''}`} onClick={() => setPicked(active ? null : { ticker: v.ticker, persona: p.key })} title={s ? s.reasoning : '无数据'}>{SIGNAL_GLYPH[kind]}{kind !== 'abstain' && s ? <b>{s.confidence}</b> : null}</button></td> })}</tr>)}
      <tr className="committee-foot"><th>共识</th>{verdicts.map(v => <td key={v.ticker}><strong className={`stance-${v.stance}`}>{v.consensus >= 0 ? '+' : ''}{v.consensus.toFixed(2)}</strong></td>)}</tr>
      <tr className="committee-foot"><th>一致度</th>{verdicts.map(v => <td key={v.ticker}>{pctAbs(v.agreement)}</td>)}</tr>
    </tbody></table></div>
    {pickedSignal && pickedVerdict && <div className="committee-detail"><div className="committee-detail-head"><strong>{meta.find(p => p.key === pickedSignal.persona)?.name_zh || pickedSignal.persona} · {pickedVerdict.ticker}</strong><em className={`sig-text-${pickedSignal.abstained ? 'abstain' : pickedSignal.signal}`}>{SIGNAL_LABEL[pickedSignal.abstained ? 'abstain' : pickedSignal.signal]} {pickedSignal.abstained ? '' : `${pickedSignal.confidence}%`}</em><span>得分 {num(pickedSignal.score)} / {pickedSignal.max_score}{pickedSignal.margin_of_safety != null ? ` · 安全边际 ${pct(pickedSignal.margin_of_safety, 0)}` : ''}</span></div>
      {pickedSignal.abstained ? <p className="lab-note">{pickedSignal.reasoning}</p> : null}
      {pickedSignal.parts.length > 0 && <div className="committee-parts">{pickedSignal.parts.map(part => <div key={part.name}><div className="committee-part-head"><span>{part.name.replaceAll('_', ' ')}</span><strong>{num(part.score, part.score % 1 ? 2 : 0)} / {part.max_score}</strong></div><div className="risk-track"><i style={{ width: `${part.max_score > 0 ? Math.max(0, Math.min(100, (part.score / part.max_score) * 100)) : 0}%` }}/></div><p>{part.details || '—'}</p></div>)}</div>}
      {(() => { const key = `${pickedSignal.persona}|${pickedVerdict.ticker}`; const state = narratives[key]; const stored = pickedSignal.narrative ? { text: pickedSignal.narrative, grounded: pickedSignal.narrative_grounded ?? null } : null; const shown = state && state !== 'loading' && 'text' in state ? state : stored;
        return <div className="committee-narrate">{shown ? <p className="committee-narrative">{shown.text}<em className={shown.grounded === false ? 'ungrounded' : ''}>{shown.grounded === false ? '⚠ 有数字无法溯源' : shown.grounded ? '✓ 数字已溯源' : ''}</em></p> : null}{state && state !== 'loading' && 'error' in state ? <small className="committee-gaps">解读失败：{state.error}</small> : null}{!pickedSignal.abstained ? <button type="button" className="text-action" disabled={state === 'loading'} onClick={() => void narrate(pickedVerdict.ticker, pickedSignal.persona)}>{state === 'loading' ? '解读中…' : shown ? '重新解读' : 'LLM 解读'}</button> : null}</div> })()}
      {pickedSignal.data_gaps.length > 0 && <small className="committee-gaps">数据缺口：{pickedSignal.data_gaps.join('；')}</small>}
    </div>}
    <div className="lab-foot"><button type="button" className="explain-button" onClick={() => ask(`投资人委员会结果（${result.as_of}）：${verdicts.slice(0, 8).map(v => `${v.ticker} 共识${v.consensus >= 0 ? '+' : ''}${v.consensus.toFixed(2)}（${v.bullish}多/${v.bearish}空/${v.neutral}中）`).join('；')}。请解释分歧最大的股票为什么投资人意见不一。`, '实验室 · 投资人委员会')}>问 AI 解释分歧</button><RawJson data={result}/></div>
  </>;
}

// -------------------------------------------------------------------------- backtest

function EquityLine({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const min = Math.min(...values); const max = Math.max(...values); const span = Math.max(max - min, 1e-9);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 760},${150 - ((v - min) / span) * 130}`).join(' ');
  return <svg className="lab-equity" viewBox="0 0 760 160" role="img" aria-label="回测净值曲线"><polyline className="chart-line" points={pts}/></svg>;
}

function BacktestTool({ result, setResult, handoff, clearHandoff, ask }: ToolProps & { result?: BacktestResult; setResult: (r?: BacktestResult) => void; handoff: Handoff | null; clearHandoff: () => void }) {
  const [universe, setUniverse] = useState<Universe>('custom'); const [tickers, setTickers] = useState('AAPL, MSFT, NVDA');
  const [holding, setHolding] = useState('5'); const [earnings, setEarnings] = useState('8'); const [capital, setCapital] = useState('100000'); const [perTrade, setPerTrade] = useState('10000');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const run = async () => { setBusy(true); setError(''); try { setResult(await apiJson<BacktestResult>('/api/lab/backtest', { method: 'POST', body: JSON.stringify({ universe, tickers: universe === 'custom' ? parseTickers(tickers) : [], strategy: 'pead', holding_days: Number(holding), earnings_limit: Number(earnings), capital: Number(capital), per_trade: Number(perTrade) }) })) } catch (e) { setError(errorText(e)) } finally { setBusy(false) } };
  const m = result?.metrics;
  return <>
    {handoff && <HandoffBanner handoff={handoff} onUse={() => { setUniverse('custom'); setTickers(handoff.tickers.join(', ')); clearHandoff() }} onClear={clearHandoff}/>}
    <div className="lab-tool">
      <section className="surface lab-config"><div className="surface-header"><div><h2>回测参数</h2><span>策略：PEAD 财报后漂移（目前唯一已实现的策略）</span></div></div>
        <UniversePicker universe={universe} setUniverse={setUniverse} tickers={tickers} setTickers={setTickers} exclude={INDEX_UNIVERSES}/>
        <div className="lab-grid2"><Field label="每只回看财报数" hint="每份财报是一个入场事件"><NumberInput value={earnings} onChange={setEarnings} min={1} max={20}/></Field><Field label="持有交易日"><NumberInput value={holding} onChange={setHolding} min={1} max={60}/></Field><Field label="初始资金（$）"><NumberInput value={capital} onChange={setCapital} min={1000} step={10000}/></Field><Field label="单笔资金（$）"><NumberInput value={perTrade} onChange={setPerTrade} min={100} step={1000}/></Field></div>
        <button className="run-button" disabled={busy || (universe === 'custom' && !parseTickers(tickers).length)} onClick={() => void run()}>{busy ? '回测中…' : '运行回测'}</button>
        <p className="lab-note">13 位投资人的打分规则尚未接入回测；接入后这里会多出一个「策略」下拉。</p>
      </section>
      <section className="surface lab-result">
        {error ? <ErrorBox text={error}/> : !result ? <Empty glyph="↗" title="等待回测" text="在选定股票池的历史财报事件上按 PEAD 规则入场、持有 N 日出场，得到逐笔交易与净值曲线。"/> : <>
          <div className="surface-header"><div><h2>{result.strategy.toUpperCase()} · {result.tickers.length} 只 · {m?.n_trades ?? 0} 笔</h2><span>{UNIVERSE_LABEL[result.universe] || result.universe} · 持有 {result.params?.holding_days} 日 · 每只 {result.params?.earnings_limit} 份财报 · 单笔 ${Number(result.params?.per_trade || 0).toLocaleString()}</span></div></div>
          {m ? <div className="lab-stats"><Stat label="总收益" value={pct(m.total_return_pct)} tone={m.total_return_pct >= 0 ? 'positive' : 'negative'}/><Stat label="年化" value={pct(m.annualized_return_pct)}/><Stat label="夏普" value={num(m.sharpe_ratio)}/><Stat label="最大回撤" value={pct(m.max_drawdown_pct)} tone="negative"/><Stat label="胜率" value={pctAbs(m.win_rate)}/><Stat label="平均单笔" value={pct(m.avg_return_pct)}/><Stat label="多 / 空" value={`${m.n_long} / ${m.n_short}`}/><Stat label="平均持有" value={`${num(m.avg_holding_days, 1)} 日`}/></div> : <p className="lab-note">没有产生交易：股票池里可能没有可用的财报事件。</p>}
          <EquityLine values={result.equity_curve || []}/>
          {result.trades.length > 0 && <div className="lab-table-wrap"><table className="lab-table"><thead><tr><th>股票</th><th>方向</th><th>入场</th><th>出场</th><th>入场价</th><th>出场价</th><th>收益</th><th>盈亏</th></tr></thead><tbody>{result.trades.slice(0, 40).map((t, i) => <tr key={i}><td><strong>{t.ticker}</strong></td><td>{t.direction === 'long' ? '多' : '空'}</td><td>{t.entry_date}</td><td>{t.exit_date}</td><td>${num(t.entry_price)}</td><td>${num(t.exit_price)}</td><td className={t.return_pct >= 0 ? 'positive' : 'negative'}>{pct(t.return_pct)}</td><td className={t.pnl >= 0 ? 'positive' : 'negative'}>${t.pnl.toFixed(0)}</td></tr>)}</tbody></table>{result.trades.length > 40 ? <p className="lab-note">只显示前 40 笔，共 {result.trades.length} 笔。</p> : null}</div>}
          <div className="lab-foot"><button type="button" className="explain-button" onClick={() => ask(`PEAD 回测结果：${result.tickers.join(', ')}，${m?.n_trades ?? 0} 笔，总收益 ${pct(m?.total_return_pct)}，夏普 ${num(m?.sharpe_ratio)}，最大回撤 ${pct(m?.max_drawdown_pct)}，胜率 ${pctAbs(m?.win_rate)}。请评价这组指标的稳健性和样本量问题。`, '实验室 · 策略回测')}>问 AI 评价稳健性</button><RawJson data={result}/></div>
        </>}
      </section>
    </div>
  </>;
}

// ----------------------------------------------------------------------- event study

function EventStudyTool({ result, setResult, handoff, clearHandoff, ask }: ToolProps & { result?: EventStudyResult; setResult: (r?: EventStudyResult) => void; handoff: Handoff | null; clearHandoff: () => void }) {
  const [universe, setUniverse] = useState<Universe>('custom'); const [tickers, setTickers] = useState('AAPL, MSFT, NVDA');
  const [earnings, setEarnings] = useState('8'); const [boot, setBoot] = useState('2000'); const [surprise, setSurprise] = useState(true);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const run = async () => { setBusy(true); setError(''); try { setResult(await apiJson<EventStudyResult>('/api/lab/event-study', { method: 'POST', body: JSON.stringify({ universe, tickers: universe === 'custom' ? parseTickers(tickers) : [], earnings_limit: Number(earnings), n_bootstrap: Number(boot), require_eps_surprise: surprise }) })) } catch (e) { setError(errorText(e)) } finally { setBusy(false) } };
  return <>
    {handoff && <HandoffBanner handoff={handoff} onUse={() => { setUniverse('custom'); setTickers(handoff.tickers.join(', ')); clearHandoff() }} onClear={clearHandoff}/>}
    <div className="lab-tool">
      <section className="surface lab-config"><div className="surface-header"><div><h2>事件研究参数</h2><span>事件 = 财报公布；基准 = 市场模型（SPY）</span></div></div>
        <UniversePicker universe={universe} setUniverse={setUniverse} tickers={tickers} setTickers={setTickers} exclude={INDEX_UNIVERSES}/>
        <div className="lab-grid2"><Field label="每只回看财报数"><NumberInput value={earnings} onChange={setEarnings} min={1} max={20}/></Field><Field label="Bootstrap 次数"><NumberInput value={boot} onChange={setBoot} min={100} max={10000} step={500}/></Field></div>
        <Field label="只统计有 EPS surprise 标注的事件"><Chips options={[{ id: 'yes', label: '是' }, { id: 'no', label: '否' }]} value={surprise ? 'yes' : 'no'} onChange={v => setSurprise(v === 'yes')}/></Field>
        <button className="run-button" disabled={busy || (universe === 'custom' && !parseTickers(tickers).length)} onClick={() => void run()}>{busy ? '计算中…' : '开始计算'}</button>
      </section>
      <section className="surface lab-result">
        {error ? <ErrorBox text={error}/> : !result ? <Empty glyph="∿" title="等待计算" text="对每次财报公布，用市场模型剔除大盘影响后累计 [0,+1]、[0,+5]、[0,+20] 日的异常收益，并给出 t 检验和 bootstrap 置信区间。"/> : <>
          <div className="surface-header"><div><h2>{result.events.length} 个事件 · {result.aggregates.length} 组</h2><span>{UNIVERSE_LABEL[result.universe] || result.universe} · {result.tickers.join(' ')}{result.skipped_tickers.length ? ` · 跳过 ${result.skipped_tickers.join(' ')}` : ''}</span></div></div>
          {result.aggregates.map(g => <div key={g.source_type} className="lab-table-wrap"><div className="lab-subhead">{g.source_type} · {g.n_events} 个事件</div><table className="lab-table"><thead><tr><th>窗口</th><th>n</th><th>平均 CAR</th><th>标准差</th><th>t</th><th>p</th><th>95% CI</th></tr></thead><tbody>{g.windows.map(w => <tr key={w.window}><td><strong>{w.window}</strong></td><td>{w.n_events}</td><td className={w.mean_car >= 0 ? 'positive' : 'negative'}>{pct(w.mean_car, 2)}</td><td>{pctAbs(w.std_car, 2)}</td><td>{num(w.t_stat)}</td><td className={w.p_value < 0.05 ? 'positive' : ''}>{num(w.p_value, 3)}</td><td>[{pct(w.ci.lower, 2)}, {pct(w.ci.upper, 2)}]</td></tr>)}</tbody></table></div>)}
          {result.events.length > 0 && <div className="lab-table-wrap"><div className="lab-subhead">逐事件</div><table className="lab-table"><thead><tr><th>股票</th><th>日期</th><th>类型</th><th>EPS</th><th>CAR [0,1]</th><th>CAR [0,5]</th><th>CAR [0,20]</th><th>β</th></tr></thead><tbody>{result.events.slice(0, 40).map((e, i) => <tr key={i}><td><strong>{e.ticker}</strong></td><td>{e.event_date}</td><td>{e.source_type}</td><td>{e.eps_surprise || '—'}</td><td className={(e.car_0_1 || 0) >= 0 ? 'positive' : 'negative'}>{pct(e.car_0_1, 2)}</td><td className={(e.car_0_5 || 0) >= 0 ? 'positive' : 'negative'}>{pct(e.car_0_5, 2)}</td><td className={(e.car_0_20 || 0) >= 0 ? 'positive' : 'negative'}>{pct(e.car_0_20, 2)}</td><td>{num(e.market_model.beta)}</td></tr>)}</tbody></table></div>}
          <div className="lab-foot"><button type="button" className="explain-button" onClick={() => ask(`事件研究：${result.tickers.join(', ')}，${result.events.length} 个财报事件。${result.aggregates.map(g => `${g.source_type}: ${g.windows.map(w => `${w.window} 平均CAR ${pct(w.mean_car, 2)} (p=${num(w.p_value, 3)})`).join('，')}`).join('；')}。请解释这些窗口的统计显著性意味着什么。`, '实验室 · 事件研究')}>问 AI 解释显著性</button><RawJson data={result}/></div>
        </>}
      </section>
    </div>
  </>;
}

// ------------------------------------------------------------------------ scoreboard

function ScoreboardTool({ ask }: ToolProps) {
  const board = useLabData<Scoreboard>('/api/lab/committee/scoreboard');
  const thresholds = useLabData<{ monitoring: Record<string, number>; intraday: Record<string, number> }>('/api/lab/signals');
  const [busy, setBusy] = useState(false); const [report, setReport] = useState<{ checked: number; filled: number; skipped_no_price: number; errors: Record<string, string> } | null>(null); const [error, setError] = useState('');
  const backfill = async () => { setBusy(true); setError(''); try { setReport(await apiJson('/api/lab/committee/backfill', { method: 'POST', body: JSON.stringify({}) })); board.reload() } catch (e) { setError(errorText(e)) } finally { setBusy(false) } };
  const c = board.data?.counts;
  return <div className="lab-tool">
    <section className="surface lab-config"><div className="surface-header"><div><h2>观察进度</h2><span>投票时只有当天数据，收益是之后才发生的</span></div></div>
      {c ? <div className="lab-stats one"><Stat label="委员会运行" value={String(c.runs)}/><Stat label="覆盖股票" value={String(c.tickers)}/><Stat label="有效票" value={String(c.votes)}/><Stat label="已评 1 月 / 待评" value={`${c.scored_1m} / ${c.due_1m}`}/><Stat label="已评 3 月 / 待评" value={`${c.scored_3m} / ${c.due_3m}`}/></div> : board.error ? <ErrorBox text={board.error}/> : null}
      <button className="run-button" disabled={busy} onClick={() => void backfill()}>{busy ? '回填中…' : '立即回填到期的票'}</button>
      <p className="lab-note">调度器每天 02:30 ET 自动回填一次；这里只是手动触发同一段代码。</p>
      {report && <p className="lab-note">本次检查 {report.checked}，回填 {report.filled}，缺价格 {report.skipped_no_price}{Object.keys(report.errors).length ? `，错误 ${Object.keys(report.errors).length}` : ''}。</p>}
      {error && <ErrorBox text={error}/>}
      {thresholds.data && <div className="lab-thresholds"><div className="lab-subhead">生产监控阈值（只读）</div>{Object.entries({ ...thresholds.data.intraday, volume_spike_threshold: thresholds.data.monitoring.volume_spike_threshold, insider_buy_min_value: thresholds.data.monitoring.insider_buy_min_value }).map(([k, v]) => <div key={k}><span>{k.replaceAll('_', ' ')}</span><strong>{typeof v === 'number' && v < 1 ? pctAbs(v, 1) : typeof v === 'number' && v >= 10000 ? money(v) : String(v)}</strong></div>)}</div>}
    </section>
    <section className="surface lab-result">
      {!board.data ? null : board.data.items.length === 0 ? <Empty glyph="◎" title="还没有可评分的票" text={c && c.votes ? `已有 ${c.votes} 票在观察中，最早的一批在投票 30 天后进入评分。` : '先在「投资人委员会」跑几次评审，票会在 30 天和 91 天后被真实收益评分。'}/> : <>
        <div className="surface-header"><div><h2>逐人命中率（1 个月）</h2><span>命中 = 看多且上涨，或看空且下跌；中性票不计</span></div></div>
        <div className="lab-table-wrap"><table className="lab-table"><thead><tr><th>投资人</th><th>票数</th><th>命中</th><th>命中率</th><th>方向平均收益</th></tr></thead><tbody>{board.data.items.map(r => <tr key={r.persona}><td><strong>{r.name_zh || r.persona}</strong></td><td>{r.n}</td><td>{r.hits}</td><td>{pctAbs(r.hit_rate)}</td><td className={(r.avg_directional_1m || 0) >= 0 ? 'positive' : 'negative'}>{pct(r.avg_directional_1m, 2)}</td></tr>)}</tbody></table></div>
        <div className="lab-foot"><button type="button" className="explain-button" onClick={() => ask(`投资人命中率榜：${board.data!.items.map(r => `${r.name_zh || r.persona} ${pctAbs(r.hit_rate)}（${r.n} 票）`).join('，')}。样本量这么小时，该怎么解读这些差异？`, '实验室 · 观察记分板')}>问 AI 怎么解读</button></div>
      </>}
    </section>
  </div>;
}

// ------------------------------------------------------------------------------ runs

function RunsTool({ onOpen }: ToolProps & { onOpen: (kind: string, result: unknown) => void }) {
  const [kind, setKind] = useState<string>('all');
  const runs = useLabData<{ items: RunSummary[]; counts: Record<string, number> }>(`/api/lab/runs?limit=100${kind === 'all' ? '' : `&kind=${kind}`}`, [kind]);
  const [error, setError] = useState('');
  const open = async (run: RunSummary) => { setError(''); try { const row = await apiJson<{ kind: string; result: unknown }>(`/api/lab/runs/${encodeURIComponent(run.id)}`); onOpen(row.kind, row.result) } catch (e) { setError(errorText(e)) } };
  const kinds = ['all', ...Object.keys(runs.data?.counts || {})];
  return <section className="surface">
    <div className="surface-header"><div><h2>运行记录</h2><span>后端重启不丢；点一条在对应工具里原样重开</span></div><div className="lab-chips">{kinds.map(k => <button key={k} type="button" className={k === kind ? 'active' : ''} onClick={() => setKind(k)}>{k === 'all' ? `全部 ${Object.values(runs.data?.counts || {}).reduce((a, b) => a + b, 0)}` : `${KIND_LABEL[k] || k} ${runs.data?.counts[k] ?? ''}`}</button>)}</div></div>
    {error && <ErrorBox text={error}/>}{runs.error && <ErrorBox text={runs.error}/>}
    {!runs.data?.items.length ? <Empty glyph="◷" title="还没有运行记录" text="每个工具跑完都会记在这里。"/> : <div className="lab-runs">{runs.data.items.map(r => r.kind === 'backfill' ? <div key={r.id} className="lab-run static"><em className="kind-backfill">收益回填</em><span>回填 {String(r.filled)} / {String(r.checked)}</span><time>{when(r.ran_at)}</time></div> : <RunRow key={r.id} run={r} onOpen={() => void open(r)}/>)}</div>}
  </section>;
}
