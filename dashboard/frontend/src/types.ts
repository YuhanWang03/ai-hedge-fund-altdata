// Event types emitted by the backend over SSE. Keep in sync with
// v2/observability/hooks.py and app/runner/executor.py.

export type EventType =
  | 'session_start'
  | 'session_end'
  | 'module_enter'
  | 'module_exit'
  | 'intent_classified'
  | 'api_call'
  | 'llm_call'
  | 'db_write'
  | 'chat_message'
  | 'error'
  | 'stream_close'

export interface TraceEvent {
  type: EventType
  session_id?: string
  seq?: number
  ts_ms?: number
  replayed?: boolean
  cached_from?: string
  cached_at_ms?: number
  // type-specific fields are merged in
  [key: string]: unknown
}

export interface ChatMessage {
  id: string
  role: 'user' | 'bot' | 'system'
  text: string
  ts_ms: number
  intent?: string | null
  sessionId?: string
  cached?: boolean
  cachedAtMs?: number
  costUsd?: number
}

export interface QueryResponse {
  session_id: string
  sse_url: string
  intent: string
  args: Record<string, unknown>
  estimate_usd: number
  budget_remaining_usd: number | null
  rate_remaining: number | null
  cached: boolean
  cached_from?: string | null
  cached_at_ms?: number | null
}

export interface BudgetStatus {
  kind: 'owner' | 'guest'
  global_daily_used_usd: number
  global_daily_cap_usd: number
  global_daily_remaining_usd: number
  your_ip_hourly_remaining: number | null
  resets_at_utc: string
}
