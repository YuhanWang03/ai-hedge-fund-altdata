// Map a single trace event to the pipeline step ID it represents, or
// null if the event doesn't correspond to any step (e.g. module_enter,
// module_exit, session_end — these don't drive the pipeline bar).
//
// Backend already attaches `role` to llm_call events via the same
// fingerprinter event_explanations.llm_role() uses, so the frontend
// reads it directly without re-implementing prompt sniffing.

import type { TraceEvent } from './types'

function stripFdClassPrefix(endpoint: string): string {
  // Hook emits "CachedFDClient.get_prices" / "FDClient.get_prices";
  // pipeline mapping keys on the bare method name.
  if (endpoint.includes('.')) {
    const head = endpoint.split('.')[0]
    if (head.endsWith('FDClient')) return endpoint.split('.', 2)[1]
  }
  return endpoint
}

export function eventToStep(event: TraceEvent): string | null {
  // Cast to string so we can match forward-compatible event types that
  // aren't yet in the EventType union (db_read / db_query / validate /
  // reply will be added when their emit sites land).
  const type: string = event.type as string
  const provider = typeof event.provider === 'string' ? event.provider : ''
  const role = typeof event.role === 'string' ? event.role : ''
  const op = typeof event.op === 'string' ? event.op : ''
  const fn = typeof event.fn === 'string' ? event.fn : ''
  const endpoint = typeof event.endpoint === 'string' ? event.endpoint : ''

  if (type === 'session_start') return 'input'

  if (type === 'intent_classified') return 'classify'
  if (type === 'llm_call' && role === 'intent_classifier') return 'classify'

  if (type === 'api_call' && provider === 'edgar') return 'edgar'
  if (type === 'api_call' && provider === 'ark_csv') return 'ark'
  if (type === 'api_call' && provider === 'alpaca') return 'alpaca'

  if (type === 'api_call' && provider === 'fd') {
    const m = stripFdClassPrefix(endpoint)
    if (m === 'get_prices') return 'price'
    if (m === 'get_financial_metrics' || m === 'get_earnings' || m === 'get_company_facts') return 'fundamentals'
    if (m === 'get_insider_trades') return 'insider'
  }

  if (type === 'api_call' && provider === 'tavily') return 'news'

  if (type === 'transform' && op === 'cusip_aggregate') return 'aggregate'
  if (type === 'transform' && (op === 'detect_changes' || op === 'etf_diff')) return 'detect'
  if (type === 'transform' && (op === 'entity_filter' || op === 'source_tier_score' || op === 'filter')) return 'filter'

  if (type === 'llm_call' && role === 'interpret_changes') return 'llm_interpret'
  if (type === 'llm_call' && role === 'narrator') return 'llm_interpret'
  if (type === 'llm_call' && role === 'proposer') return 'llm_propose'
  if (type === 'llm_call' && role === 'verifier') return 'verify'
  if (type === 'llm_call' && role === 'generator') return 'generate'

  if (type === 'db_write' && fn === 'anomaly_memory_remember') return 'memory'

  if (type === 'db_read' || type === 'db_query') return 'sqlite_read'
  if (type === 'db_write' && fn !== 'anomaly_memory_remember') return 'sqlite_write'

  if (type === 'validate') return 'validate'

  if (type === 'render') return 'render'

  if (type === 'chat_message' || type === 'reply') return 'reply'

  return null
}


// ---- Highlight helper ----------------------------------------------------

/**
 * Pure predicate used by TracePanel to decide whether a given event should
 * carry the "liquid glass" highlight ring. Gating both on session-complete
 * and on a matching step ID keeps the in-flight UX unchanged.
 */
export function shouldHighlight(
  event: TraceEvent,
  highlightedStepId: string | null,
  sessionComplete: boolean,
): boolean {
  if (!sessionComplete || !highlightedStepId) return false
  return eventToStep(event) === highlightedStepId
}


export function isSessionComplete(events: TraceEvent[], cached: boolean): boolean {
  if (cached) return true
  return events.some((e) => e.type === 'session_end')
}
