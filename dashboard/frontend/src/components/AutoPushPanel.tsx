// Read-only feed of recent automated pushes. Fetches /api/recent_pushes
// on mount, subscribes to /sse/auto_push for live updates. Clicking a
// card both (a) inline-expands the full text_html within the card and
// (b) routes the trace + push metadata into sessionStore.pushDetail so
// TracePanel can render it. Crucially, this NEVER touches the user-chat
// fields — switching back to 用户问答 mode leaves the conversation
// history intact.

import { useEffect, useRef, useState } from 'react'
import {
  fetchPushDetail,
  fetchRecentPushes,
  openAutoPushStream,
  type PushDetail,
  type PushSummary,
} from '../api/auto_push'
import { useSession } from '../store/session'
import type { TraceEvent } from '../types'

// Agent → dashboard intent. Drives PipelineBar pill selection when the
// user clicks a card.
const AGENT_TO_INTENT: Record<string, string> = {
  anomaly:          'explain_move',
  institutional:    'thirteen_f',
  lateral:          'chain',
  etf:              'etf_view',
  screen:           'summary',
  // Streamer-fired (minute-level intraday)
  alert:            'alert_fire',
  intraday_anomaly: 'intraday_anomaly',
}

const AGENT_ICON: Record<string, string> = {
  anomaly:          '⚡',
  institutional:    '🏛',
  lateral:          '🕸',
  etf:              '📈',
  screen:           '📋',
  intraday:         '⚡',
  intraday_anomaly: '⚡',
  alert:            '🔔',
}


function formatTs(ts: string): string {
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
  // Per-card expansion state: id → full PushDetail (or 'loading').
  const [expanded, setExpanded] = useState<Record<number, PushDetail | 'loading'>>({})
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const setPushDetail = useSession((s) => s.setPushDetail)
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
    )
    return () => {
      cancelled = true
      close()
    }
  }, [])

  const onCardClick = async (p: PushSummary) => {
    setSelectedId(p.id)
    // Toggle: clicking the already-expanded card collapses + clears the
    // trace view.
    if (expanded[p.id] && expanded[p.id] !== 'loading') {
      setExpanded((prev) => {
        const next = { ...prev }
        delete next[p.id]
        return next
      })
      setPushDetail(null)
      setSelectedId(null)
      return
    }
    setExpanded((prev) => ({ ...prev, [p.id]: 'loading' }))
    try {
      const detail = await fetchPushDetail(p.id)
      setExpanded((prev) => ({ ...prev, [p.id]: detail }))
      // Route into TracePanel via pushDetail (NOT user-chat). ChatPanel
      // is unaffected.
      setPushDetail({
        pushId: p.id,
        intent: AGENT_TO_INTENT[p.agent] ?? null,
        events: detail.events as TraceEvent[],
        push: {
          ts: detail.push.ts,
          agent: detail.push.agent,
          title: detail.push.title,
          text_html: detail.push.text_html,
          tickers: detail.push.tickers,
        },
      })
    } catch (err) {
      setError(`failed to load push ${p.id}: ${(err as Error).message}`)
      setExpanded((prev) => {
        const next = { ...prev }
        delete next[p.id]
        return next
      })
    }
  }

  const headerText = loading
    ? '📡 加载中…'
    : `📡 最近 ${pushes.length} 条推送（过去 2 天） · 每日 02:00 UTC 清理`

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
        {!loading && !error && pushes.length === 0 && (
          <div className="text-slate-400 text-sm text-center mt-12">
            暂无推送（开盘后 17:00 ET 起会逐步入账）
          </div>
        )}
        {pushes.map((p) => {
          const isSelected = selectedId === p.id
          const expansion = expanded[p.id]
          const isExpanded = expansion && expansion !== 'loading'
          const isLoadingThis = expansion === 'loading'
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
              {/* Preview: always shown until expanded */}
              {p.preview && !isExpanded && (
                <div
                  className="text-[12px] text-slate-600 whitespace-pre-wrap line-clamp-4 leading-snug"
                  dangerouslySetInnerHTML={{ __html: p.preview }}
                />
              )}
              {/* Expanded full HTML, inline within the card */}
              {expansion && expansion !== 'loading' && (
                <div
                  className="text-[13px] text-slate-800 whitespace-pre-wrap leading-relaxed border-t border-slate-100 pt-3"
                  dangerouslySetInnerHTML={{ __html: expansion.push.text_html ?? p.preview }}
                />
              )}
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-slate-400">
                  {p.has_trace ? '✓ 有完整 Trace' : '（无 Trace）'}
                </span>
                <span className={isSelected ? 'text-blue-700 font-medium' : 'text-slate-500'}>
                  {isLoadingThis
                    ? '… 加载中'
                    : isExpanded
                      ? '✓ 已展开 · 再次点击收起'
                      : '点击展开完整内容 + 查看 Trace →'}
                </span>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}
