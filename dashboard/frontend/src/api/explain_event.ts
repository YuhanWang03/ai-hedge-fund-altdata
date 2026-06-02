import { getOwnerToken } from './client'

export interface ExplainEventResponse {
  explanation: string
  cached: boolean
}

/**
 * Owner-only natural-language event explainer. Backed by an in-memory
 * LRU on the server, so repeated event shapes return instantly with
 * cached=true.
 */
export async function fetchEventExplanation(
  event: Record<string, unknown>,
  intent: string | null,
): Promise<ExplainEventResponse> {
  const token = getOwnerToken()
  if (!token) {
    // The component shouldn't reach this code path (it gates on owner)
    // but fail explicitly if we ever do.
    throw new Error('owner-only')
  }
  const r = await fetch('/api/explain_event_llm', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Owner-Token': token,
    },
    body: JSON.stringify({ event, intent }),
  })
  if (!r.ok) {
    throw new Error(`HTTP ${r.status}`)
  }
  return r.json()
}
