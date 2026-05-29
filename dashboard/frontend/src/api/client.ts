import type { BudgetStatus, QueryResponse } from '../types'

const OWNER_TOKEN_KEY = 'dashboard:owner_token'

export function getOwnerToken(): string | null {
  return localStorage.getItem(OWNER_TOKEN_KEY)
}

export function setOwnerToken(token: string | null) {
  if (token) localStorage.setItem(OWNER_TOKEN_KEY, token)
  else localStorage.removeItem(OWNER_TOKEN_KEY)
}

function authHeaders(): Record<string, string> {
  const t = getOwnerToken()
  return t ? { 'X-Owner-Token': t } : {}
}

export interface QueryError {
  status: number
  error: string
  message: string
  retry_after_s?: number
  resets_at_utc?: string
}

export async function postQuery(text: string): Promise<QueryResponse> {
  const resp = await fetch('/api/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ text }),
  })
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}))
    const d = (detail.detail ?? detail) as Record<string, unknown>
    const err: QueryError = {
      status: resp.status,
      error: String(d.error ?? 'unknown_error'),
      message: String(d.message ?? resp.statusText),
      retry_after_s: typeof d.retry_after_s === 'number' ? d.retry_after_s : undefined,
      resets_at_utc: typeof d.resets_at_utc === 'string' ? d.resets_at_utc : undefined,
    }
    throw err
  }
  return resp.json()
}

export async function fetchBudgetStatus(): Promise<BudgetStatus> {
  const resp = await fetch('/api/budget/status', { headers: authHeaders() })
  if (!resp.ok) throw new Error('budget_status_failed')
  return resp.json()
}
