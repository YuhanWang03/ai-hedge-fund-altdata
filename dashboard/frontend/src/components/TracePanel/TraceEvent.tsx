import type { TraceEvent as Ev } from '../../types'

interface Props {
  event: Ev
  // When set (non-null), forces the explanation disclosure to that state.
  // Bumped from TracePanel via the global toggle.
  forceExplanationOpen?: boolean | null
  // Increments on each global toggle so React remounts the <details>
  // element and the new initial `open` value sticks.
  explanationBump?: number
  // When true, wraps the row in a liquid-glass highlight ring. Driven
  // by TracePanel via the pipeline pill click after session completion.
  highlighted?: boolean
}

// Row shape inside transform/detect_changes events.
interface ChangeRow {
  ticker: string
  issuer: string
  action: 'new' | 'exit' | 'increase' | 'decrease' | string
  current_value: number
  change_value: number
}

// Human-readable verbs + the chip color for each ChangeType.
const ACTION_META: Record<string, { label: string; chip: string; sign: '+' | '-' | '' }> = {
  new:      { label: '🟢 新进',   chip: 'bg-green-100 text-green-800 border-green-300',   sign: '+' },
  increase: { label: '🟢 加仓',   chip: 'bg-green-100 text-green-800 border-green-300',   sign: '+' },
  decrease: { label: '🟡 减仓',   chip: 'bg-yellow-100 text-yellow-900 border-yellow-300', sign: '-' },
  exit:     { label: '🔴 清仓',   chip: 'bg-red-100 text-red-800 border-red-300',         sign: '-' },
}

function shortMoney(v: number): string {
  const sign = v < 0 ? '-' : ''
  const abs = Math.abs(v)
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`
  return `${sign}$${abs.toFixed(0)}`
}

function ChangesTable({ changes }: { changes: ChangeRow[] }) {
  return (
    <div className="mt-2 border border-violet-200 rounded overflow-hidden">
      <table className="w-full mono text-[11px] leading-tight">
        <tbody>
          {changes.map((c, i) => {
            const meta = ACTION_META[c.action] ?? { label: c.action, chip: 'bg-ink-100 text-ink-700 border-ink-200', sign: '' as const }
            return (
              <tr key={`${c.ticker}_${i}`} className={i % 2 ? 'bg-violet-50/40' : 'bg-white/60'}>
                <td className="px-2 py-1 font-medium text-ink-800 w-16 truncate">{c.ticker}</td>
                <td className="px-2 py-1 text-ink-600 truncate">{c.issuer}</td>
                <td className="px-2 py-1 w-20 text-right">
                  <span className={`inline-block px-1.5 py-0.5 rounded border ${meta.chip}`}>
                    {meta.label}
                  </span>
                </td>
                <td className="px-2 py-1 w-20 text-right text-ink-800">{shortMoney(c.current_value)}</td>
                <td className="px-2 py-1 w-20 text-right text-ink-600">
                  {c.change_value !== 0 ? `${c.change_value > 0 ? '+' : ''}${shortMoney(c.change_value)}` : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// Event fields that are wire-level metadata or rendered by dedicated UI,
// never as a generic chip. Used by the transform / render branches when
// they iterate over the remaining payload.
const RESERVED_FIELDS = new Set<string>([
  // Wire envelope.
  'type', 'session_id', 'seq', 'ts_ms',
  // Replay annotations.
  'replayed', 'cached_from', 'cached_at_ms',
  // Rendered by the dedicated 📖 解析 disclosure below.
  'explanation',
  // Already used as the heading of transform / render bodies.
  'op', 'card',
  // detect_changes' rows are rendered by the ChangesTable, not as a chip.
  'changes',
])

const MONEY_FIELD = /(?:_usd|value|cost|amount|spend|budget)/i

function formatValue(v: unknown, key?: string): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') {
    if (key && MONEY_FIELD.test(key)) {
      if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`
      if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`
      if (v >= 1e3) return `$${(v / 1e3).toFixed(1)}K`
      return `$${v.toFixed(2)}`
    }
    if (Number.isInteger(v) && Math.abs(v) >= 1e3) return v.toLocaleString()
    return String(v)
  }
  if (typeof v === 'string') return v
  return JSON.stringify(v)
}

const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  session_start:     { label: '▶ session start', color: 'text-ink-500' },
  session_end:       { label: '■ session end',   color: 'text-ink-500' },
  module_enter:      { label: '┌ module',        color: 'text-accent-module' },
  module_exit:       { label: '└ module exit',   color: 'text-accent-module' },
  intent_classified: { label: '◆ intent',        color: 'text-accent-intent' },
  api_call:          { label: '↗ api',           color: 'text-accent-fd' },
  llm_call:          { label: '✻ llm',           color: 'text-accent-llm' },
  db_write:          { label: '▾ db write',      color: 'text-accent-db' },
  transform:         { label: '⟳ transform',     color: 'text-violet-600' },
  render:            { label: '▦ render',        color: 'text-teal-600' },
  chat_message:      { label: '✉ reply',         color: 'text-ink-500' },
  error:             { label: '✗ error',         color: 'text-red-600' },
}

export function TraceEvent({ event, forceExplanationOpen, explanationBump, highlighted }: Props) {
  const meta = TYPE_LABELS[event.type] ?? { label: event.type, color: 'text-ink-500' }

  // Type-specific body renderers
  let body: React.ReactNode = null

  if (event.type === 'api_call') {
    const provider = String(event.provider ?? '?')
    const colorByProvider: Record<string, string> = {
      fd: 'bg-blue-50 border-blue-200 text-blue-900',
      tavily: 'bg-emerald-50 border-emerald-200 text-emerald-900',
      alpaca: 'bg-cyan-50 border-cyan-200 text-cyan-900',
      edgar: 'bg-slate-50 border-slate-300 text-slate-800',
    }
    const css = colorByProvider[provider] ?? 'bg-ink-100 border-ink-200 text-ink-800'
    const isEmptyResult =
      typeof event.num_results === 'number' && event.num_results === 0
    body = (
      <div className={`mono text-xs px-3 py-2 rounded border ${css}`}>
        <div className="flex items-center justify-between">
          <span className="font-medium uppercase">{provider}</span>
          <span className="text-ink-500">
            {typeof event.elapsed_ms === 'number' ? `${event.elapsed_ms}ms` : ''}
            {event.cache ? ` · ${event.cache}` : ''}
          </span>
        </div>
        <div className="text-ink-700 mt-0.5 truncate">
          {String(event.endpoint ?? '')}
          {event.ticker ? `  ·  ${event.ticker}` : ''}
          {event.query ? `  ·  "${event.query}"` : ''}
          {typeof event.num_results === 'number' ? `  ·  ${event.num_results} results` : ''}
        </div>
        {/* Surface empty-result and error states so silent FD failures
            ("3ms / 0 results") are visible at a glance instead of the
            user having to chase the resulting "No data" reply downstream. */}
        {isEmptyResult && !event.error && (
          <div className="text-amber-700 mt-1 text-[11px]">
            ⚠️ {typeof event.hint === 'string' && event.hint
              ? event.hint
              : '接口返回 0 条结果 —— 可能是 API key 缺失、日期超出可用范围，或 ticker 不在 FD 覆盖里'}
          </div>
        )}
        {typeof event.error === 'string' && event.error && (
          <div className="text-red-700 mt-1 text-[11px] break-all">
            ✗ {event.error}
          </div>
        )}
        {typeof event.cost_usd === 'number' && event.cost_usd > 0 && (
          <div className="text-ink-400 text-[10px] mt-0.5">${event.cost_usd.toFixed(5)}</div>
        )}
      </div>
    )
  } else if (event.type === 'llm_call') {
    body = (
      <div className="mono text-xs px-3 py-2 rounded border bg-purple-50 border-purple-200 text-purple-900">
        <div className="flex items-center justify-between">
          <span className="font-medium">{String(event.model ?? 'llm')}</span>
          <span className="text-ink-500">
            {typeof event.input_tokens === 'number' && `${event.input_tokens} in / `}
            {typeof event.output_tokens === 'number' && `${event.output_tokens} out`}
            {typeof event.elapsed_ms === 'number' ? `  ·  ${event.elapsed_ms}ms` : ''}
          </span>
        </div>
        {typeof event.prompt_preview === 'string' && event.prompt_preview && (
          <details className="mt-1">
            <summary className="text-ink-600 cursor-pointer">prompt</summary>
            <pre className="text-ink-700 whitespace-pre-wrap pt-1 break-all">
              {event.prompt_preview}
            </pre>
          </details>
        )}
        {typeof event.response_preview === 'string' && event.response_preview && (
          <details className="mt-1">
            <summary className="text-ink-600 cursor-pointer">response</summary>
            <pre className="text-ink-700 whitespace-pre-wrap pt-1 break-all">
              {event.response_preview}
            </pre>
          </details>
        )}
        {typeof event.cost_usd === 'number' && event.cost_usd > 0 && (
          <div className="text-ink-400 text-[10px] mt-1">${event.cost_usd.toFixed(5)}</div>
        )}
      </div>
    )
  } else if (event.type === 'db_write') {
    body = (
      <div className="mono text-xs px-3 py-2 rounded border bg-amber-50 border-amber-200 text-amber-900">
        <div className="flex items-center justify-between">
          <span className="font-medium uppercase">{String(event.db ?? 'db')}</span>
          <span className="text-ink-500">
            {typeof event.elapsed_ms === 'number' ? `${event.elapsed_ms}ms` : ''}
          </span>
        </div>
        <div className="text-ink-700 mt-0.5">{String(event.fn ?? '')}</div>
      </div>
    )
  } else if (event.type === 'intent_classified') {
    body = (
      <div className="mono text-xs px-3 py-2 rounded border bg-red-50 border-red-200 text-red-900">
        <div className="flex items-center justify-between">
          <span className="font-medium">{String(event.intent ?? '?')}</span>
          <span className="text-ink-500">
            {typeof event.elapsed_ms === 'number' ? `${event.elapsed_ms}ms` : ''}
          </span>
        </div>
        <div className="text-ink-700 mt-0.5">
          input: <span className="text-ink-500">"{String(event.input_text ?? '')}"</span>
        </div>
        {event.args ? (
          <div className="text-ink-700 mt-0.5">
            args: <span className="text-ink-500">{JSON.stringify(event.args)}</span>
          </div>
        ) : null}
      </div>
    )
  } else if (event.type === 'module_enter' || event.type === 'module_exit') {
    const isEntry = event.type === 'module_enter'
    body = (
      <div className={
        'mono text-xs px-3 py-2 rounded border ' +
        (isEntry
          ? 'bg-indigo-50 border-indigo-200 text-indigo-900'
          : 'bg-ink-50 border-ink-200 text-ink-600')
      }>
        <div className="flex items-center justify-between">
          <span className="font-medium">{String(event.name ?? '')}</span>
          <span className="text-ink-500">
            {typeof event.elapsed_ms === 'number' ? `${event.elapsed_ms}ms` : ''}
          </span>
        </div>
        {isEntry && typeof event.intent === 'string' && (
          <div className="text-ink-600 mt-0.5">
            intent: <span className="text-ink-500">{String(event.intent)}</span>
          </div>
        )}
      </div>
    )
  } else if (event.type === 'error') {
    body = (
      <div className="mono text-xs px-3 py-2 rounded border bg-red-50 border-red-200 text-red-900">
        <div className="font-medium">{String(event.where ?? 'error')}</div>
        <div>{String(event.message ?? '')}</div>
      </div>
    )
  } else if (event.type === 'transform') {
    // Internal data transformation — CUSIP aggregation, change detection, etc.
    const op = String(event.op ?? '?')
    // detect_changes carries a `changes` array we render as a compact table.
    const changes = Array.isArray(event.changes) ? (event.changes as ChangeRow[]) : null
    const fields = Object.entries(event).filter(([k]) => !RESERVED_FIELDS.has(k))
    body = (
      <div className="mono text-xs px-3 py-2 rounded border bg-violet-50 border-violet-200 text-violet-900">
        <div className="font-medium">{op}</div>
        <div className="text-ink-700 mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
          {fields.length === 0 ? <span className="text-ink-400">(no fields)</span> : fields.map(([k, v]) => (
            <span key={k}>
              {k}: <span className="text-ink-500">{formatValue(v, k)}</span>
            </span>
          ))}
        </div>
        {changes && changes.length > 0 && <ChangesTable changes={changes} />}
      </div>
    )
  } else if (event.type === 'render') {
    const card = String(event.card ?? '?')
    const fields = Object.entries(event).filter(([k]) => !RESERVED_FIELDS.has(k))
    body = (
      <div className="mono text-xs px-3 py-2 rounded border bg-teal-50 border-teal-200 text-teal-900">
        <div className="font-medium">{card}</div>
        <div className="text-ink-700 mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
          {fields.length === 0 ? <span className="text-ink-400">(no fields)</span> : fields.map(([k, v]) => (
            <span key={k}>
              {k}: <span className="text-ink-500">{formatValue(v, k)}</span>
            </span>
          ))}
        </div>
      </div>
    )
  } else if (event.type === 'chat_message') {
    body = (
      <div className="text-xs text-ink-500 italic">
        bot replied · projected to chat panel →
      </div>
    )
  } else if (event.type === 'session_start') {
    body = (
      <div className="text-xs text-ink-500">
        text: <span className="mono">"{String(event.text ?? '')}"</span>
      </div>
    )
  } else if (event.type === 'session_end') {
    body = (
      <div className="text-xs text-ink-500 mono">
        total ${typeof event.total_cost_usd === 'number' ? event.total_cost_usd.toFixed(5) : '0'}
        {typeof event.elapsed_ms === 'number' ? `  ·  ${event.elapsed_ms}ms` : ''}
      </div>
    )
  }

  // The product-friendly explanation panel. Default closed; can be force-
  // opened from the parent's "expand all" toggle via a key bump.
  const expl = event.explanation
  const explKey = `expl_${event.session_id ?? '?'}_${event.seq ?? 0}_${explanationBump ?? 0}`
  const explanationBlock = expl ? (
    <details
      key={explKey}
      open={forceExplanationOpen === null || forceExplanationOpen === undefined ? undefined : forceExplanationOpen}
      className="mt-2 text-xs"
    >
      <summary className="text-slate-500 cursor-pointer select-none hover:text-slate-700">
        📖 解析
      </summary>
      <div className="mt-1 pl-3 border-l-2 border-slate-200 space-y-1 text-slate-700">
        <div>📍 <span className="text-slate-500">来源</span> {expl.source}</div>
        <div>🔧 <span className="text-slate-500">方式</span> {expl.how}</div>
        <div>📦 <span className="text-slate-500">内容</span> {expl.what}</div>
        <div>💾 <span className="text-slate-500">存储</span> {expl.store}</div>
        <div>➡️ <span className="text-slate-500">下一步</span> {expl.next}</div>
      </div>
    </details>
  ) : null

  return (
    <div
      className={'flex items-start gap-2' + (highlighted ? ' glass-highlight' : '')}
      data-event-seq={event.seq}
    >
      <div className={`text-[11px] mono ${meta.color} w-32 shrink-0 pt-1`}>
        {meta.label}
      </div>
      <div className="flex-1 min-w-0">
        {body}
        {explanationBlock}
      </div>
    </div>
  )
}
