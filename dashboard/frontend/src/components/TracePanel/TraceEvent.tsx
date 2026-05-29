import type { TraceEvent as Ev } from '../../types'

interface Props { event: Ev }

const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  session_start:     { label: '▶ session start', color: 'text-ink-500' },
  session_end:       { label: '■ session end',   color: 'text-ink-500' },
  module_enter:      { label: '┌ module',        color: 'text-accent-module' },
  module_exit:       { label: '└ module exit',   color: 'text-accent-module' },
  intent_classified: { label: '◆ intent',        color: 'text-accent-intent' },
  api_call:          { label: '↗ api',           color: 'text-accent-fd' },
  llm_call:          { label: '✻ llm',           color: 'text-accent-llm' },
  db_write:          { label: '▾ db write',      color: 'text-accent-db' },
  chat_message:      { label: '✉ reply',         color: 'text-ink-500' },
  error:             { label: '✗ error',         color: 'text-red-600' },
}

export function TraceEvent({ event }: Props) {
  const meta = TYPE_LABELS[event.type] ?? { label: event.type, color: 'text-ink-500' }

  // Type-specific body renderers
  let body: React.ReactNode = null

  if (event.type === 'api_call') {
    const provider = String(event.provider ?? '?')
    const colorByProvider: Record<string, string> = {
      fd: 'bg-blue-50 border-blue-200 text-blue-900',
      tavily: 'bg-emerald-50 border-emerald-200 text-emerald-900',
      alpaca: 'bg-cyan-50 border-cyan-200 text-cyan-900',
    }
    const css = colorByProvider[provider] ?? 'bg-ink-100 border-ink-200 text-ink-800'
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
    body = (
      <div className="mono text-xs text-indigo-700">
        {String(event.name ?? '')}
        {typeof event.elapsed_ms === 'number' ? `  (${event.elapsed_ms}ms)` : ''}
      </div>
    )
  } else if (event.type === 'error') {
    body = (
      <div className="mono text-xs px-3 py-2 rounded border bg-red-50 border-red-200 text-red-900">
        <div className="font-medium">{String(event.where ?? 'error')}</div>
        <div>{String(event.message ?? '')}</div>
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

  return (
    <div className="flex items-start gap-2">
      <div className={`text-[11px] mono ${meta.color} w-32 shrink-0 pt-1`}>
        {meta.label}
      </div>
      <div className="flex-1 min-w-0">{body}</div>
    </div>
  )
}
