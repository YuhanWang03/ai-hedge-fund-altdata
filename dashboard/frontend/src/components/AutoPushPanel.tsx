// Read-only feed of recent automated pushes. Fetches /api/recent_pushes
// on mount, subscribes to /sse/auto_push for live updates, and on card
// click loads the full trace into the session store so TracePanel +
// ChatPanel both reflect the selected archive row.

import { useEffect, useRef, useState } from 'react'
import {
  fetchPushDetail,
  fetchRecentPushes,
  openAutoPushStream,
  type PushSummary,
} from '../api/auto_push'
import { useSession } from '../store/session'
import type { TraceEvent } from '../types'

// Agent name (set by each scheduler script when constructing Archive) →
// the intent ID the dashboard's PipelineBar understands. Anything not in
// the map falls back to null, which makes the pipeline bar disappear (a
// safe default — the trace still renders).
const AGENT_TO_INTENT: Record<string, string> = {
  anomaly:       'explain_move',
  institutional: 'thirteen_f',
  lateral:       'chain',
  etf:           'etf_view',
  screen:        'summary',
}

// Visual icon per agent, falls back to 🔔.
const AGENT_ICON: Record<string, string> = {
  anomaly:       '⚡',
  institutional: '🏛',
  lateral:       '🕸',
  etf:           '📈',
  screen:        '📋',
  intraday:      '⚡',
}


function formatTs(ts: string): string {
  // ISO 8601 → "MM-DD HH:MM UTC" for compactness in the card header.
  try {
    const d = new Date(ts)
    if (isNaN(d.getTime())) return ts
    const pad = (n: number) => String(n).padStart(2, '0')
    return (
      `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
      `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
    )
  } catch {
    return ts
  }
}


export function AutoPushPanel() {
  const [pushes, setPushes] = useState<PushSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [loadingTrace, setLoadingTrace] = useState(false)
  const loadPush = useSession((s) => s.loadPush)
  const containerRef = useRef<HTMLDivElement>(null)

  // Initial backfill + live SSE subscription.
  useEffect(() => {
    let cancelled = false
    fetchRecentPushes(2)
      .then((rows) => {
        if (!cancelled) {
          setPushes(rows)
          setLoading(false)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message ?? 'failed to load recent pushes')
          setLoading(false)
        }
      })

    const close = openAutoPushStream(
      (row) => {
        setPushes((prev) => {
          if (prev.some((p) => p.id === row.id)) return prev
          return [row, ...prev]
        })
      },
      () => {
        /* stream closed — handled by browser auto-reconnect */
      },
    )
    return () => {
      cancelled = true
      close()
    }
  }, [])

  const onCardClick = async (p: PushSummary) => {
    if (loadingTrace) return
    setSelectedId(p.id)
    if (!p.has_trace) {
      // Still load the card text so the chat panel renders it, even if
      // there's no trace to replay.
      try {
        setLoadingTrace(true)
        const detail = await fetchPushDetail(p.id)
        loadPush({
          sessionId: `push_${p.id}`,
          intent: AGENT_TO_INTENT[p.agent] ?? null,
          label: detail.push.title ?? p.title ?? `${p.agent} push`,
          events: [],
          replyText: detail.push.text_html,
          ts_ms: Date.parse(p.ts) || Date.now(),
        })
      } catch (err) {
        setError(`failed to load push ${p.id}: ${(err as Error).message}`)
      } finally {
        setLoadingTrace(false)
      }
      return
    }
    try {
      setLoadingTrace(true)
      const detail = await fetchPushDetail(p.id)
      loadPush({
        sessionId: `push_${p.id}`,
        intent: AGENT_TO_INTENT[p.agent] ?? null,
        label: detail.push.title ?? p.title ?? `${p.agent} push`,
        events: detail.events as TraceEvent[],
        replyText: detail.push.text_html,
        ts_ms: Date.parse(p.ts) || Date.now(),
      })
    } catch (err) {
      setError(`failed to load trace for push ${p.id}: ${(err as Error).message}`)
    } finally {
      setLoadingTrace(false)
    }
  }

  const cards = pushes
  const headerText = loading
    ? '📡 加载中…'
    : `📡 最近 ${cards.length} 条推送（过去 2 天） · 每日 02:00 UTC 清理`

  return (
    <div className="flex-1 flex flex-col bg-white min-h-0">
      <div className="px-4 py-2 border-b border-slate-100 bg-slate-50">
        <div className="text-[11px] text-slate-500">{headerText}</div>
      </div>

      <div ref={containerRef} className="flex-1 overflow-y-auto px-3 py-3 space-y-3">
        {error && (
          <div className="text-xs px-3 py-2 rounded bg-amber-50 text-amber-700 border border-amber-200">
            ⚠️ {error}
          </div>
        )}
        {!loading && !error && cards.length === 0 && (
          <div className="text-slate-400 text-sm text-center mt-12">
            暂无推送（开盘后 17:00 ET 起会逐步入账）
          </div>
        )}
        {cards.map((p) => {
          const isSelected = selectedId === p.id
          const icon = AGENT_ICON[p.agent] ?? '🔔'
          const title = p.title ?? `${p.agent} push`
          return (
            <article
              key={p.id}
              className={
                'border rounded-lg p-4 space-y-3 cursor-pointer transition-all ' +
                (isSelected
                  ? 'bg-blue-50 border-blue-300 shadow-md'
                  : 'bg-white border-slate-200 shadow-sm hover:border-slate-300 hover:shadow-md')
              }
              onClick={() => onCardClick(p)}
              data-push-id={p.id}
              data-agent={p.agent}
            >
              <header className="flex items-baseline justify-between gap-2">
                <h3 className="text-sm font-medium text-ink-800 truncate">
                  <span className="mr-1.5">{icon}</span>
                  {title}
                </h3>
                <span className="text-[11px] text-slate-500 mono shrink-0">
                  {formatTs(p.ts)}
                </span>
              </header>
              {p.preview && (
                <div
                  className="text-[12px] text-slate-600 whitespace-pre-wrap line-clamp-4 leading-snug"
                  // The preview is sanitized HTML from archive.db (which
                  // came from our own formatters). Using innerHTML keeps
                  // the bold/code tags rendered like a Telegram message.
                  dangerouslySetInnerHTML={{ __html: p.preview }}
                />
              )}
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-slate-400">
                  {p.has_trace ? '✓ 有完整 Trace' : '（无 Trace）'}
                </span>
                <span className={isSelected ? 'text-blue-700 font-medium' : 'text-slate-500'}>
                  {isSelected ? '✓ 已加载' : '点击查看完整 Trace →'}
                </span>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}
