import { getOwnerToken } from './client'

export interface PushSummary {
  id: number
  ts: string
  agent: string
  msg_type: string
  title: string | null
  tickers: string | null
  preview: string
  has_trace: 0 | 1
  importance_score?: number | null
  priority_tier?: 'P0' | 'P1' | 'P2' | 'P3' | null
}

export interface PushDetail {
  push: {
    id: number
    ts: string
    agent: string
    msg_type: string
    title: string | null
    tickers: string | null
    text_html: string | null
    image_path: string | null
  }
  events: Array<Record<string, unknown>>
}

function ownerHeaders(): Record<string, string> {
  const t = getOwnerToken()
  return t ? { 'X-Owner-Token': t } : {}
}

export async function fetchRecentPushes(
  days = 2,
  options: { includeP3?: boolean } = {},
): Promise<PushSummary[]> {
  const params = new URLSearchParams({ days: String(days) })
  if (options.includeP3) params.set('include_p3', 'true')
  const r = await fetch(`/api/recent_pushes?${params}`, {
    headers: ownerHeaders(),
  })
  if (!r.ok) throw new Error(`recent_pushes failed: ${r.status}`)
  const body = await r.json()
  return body.pushes as PushSummary[]
}

export async function fetchPushDetail(id: number): Promise<PushDetail> {
  const r = await fetch(`/api/push_trace/${id}`, { headers: ownerHeaders() })
  if (!r.ok) throw new Error(`push_trace failed: ${r.status}`)
  return r.json()
}

/**
 * Subscribe to the live auto-push SSE stream. Returns a disposer.
 *
 * EventSource doesn't support custom headers, so we can't include
 * X-Owner-Token there. The backend gates the route on the same token
 * via cookies / query? — current implementation reads from header,
 * so SSE will return 403 to the EventSource. To make this work for
 * Phase 2 we pass token as a query parameter when present.
 */
export function openAutoPushStream(
  onPush: (row: PushSummary) => void,
  onClose?: () => void,
): () => void {
  const token = getOwnerToken()
  const url = token
    ? `/sse/auto_push?token=${encodeURIComponent(token)}`
    : '/sse/auto_push'

  const es = new EventSource(url)
  es.onmessage = (raw) => {
    try {
      const row = JSON.parse(raw.data) as PushSummary
      if ((row as { type?: string }).type === 'error') return
      onPush(row)
    } catch {
      /* ignore malformed */
    }
  }
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) onClose?.()
  }
  return () => es.close()
}
