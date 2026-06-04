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
// Maps the archive.db `agent` column → PipelineBar pipeline key. Cron
// paths get dedicated *_cron pipelines because they fire a different
// event subset than the bot's on-demand variant (e.g. cron anomaly
// path skips the price-fetch step that the bot's explain_move does).
const AGENT_TO_INTENT: Record<string, string> = {
  anomaly:          'anomaly_cron',      // cron ② anomaly monitor
  institutional:    'thirteen_f',
  lateral:          'chain',
  etf:              'etf_view',
  screen:           'screen_cron',       // cron ① daily screen
  // Streamer-fired (minute-level intraday)
  alert:            'alert_fire',
  intraday_anomaly: 'intraday_anomaly',
}

type PriorityTier = 'P0' | 'P1' | 'P2' | 'P3'

const TIER_CHIP_COLOR: Record<PriorityTier, string> = {
  P0: 'bg-rose-500 text-white',
  P1: 'bg-blue-500 text-white',
  P2: 'bg-amber-400 text-amber-900',
  P3: 'bg-slate-300 text-slate-600',
}

const TIER_RANK: Record<PriorityTier, number> = { P0: 0, P1: 1, P2: 2, P3: 3 }

function tierOf(p: { priority_tier?: string | null }): PriorityTier {
  // Pre-Phase-0 archive rows have no tier — treat as P1 for ordering
  // and chip display so the feed still looks consistent.
  const t = p.priority_tier
  if (t === 'P0' || t === 'P1' || t === 'P2' || t === 'P3') return t
  return 'P1'
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
  // Pin to US Central time so the feed reads in the same zone as the
  // scheduler's market-hours reasoning. Central is UTC-5 in DST (CDT)
  // and UTC-6 outside it (CST); Intl handles the switch.
  try {
    const d = new Date(ts)
    if (isNaN(d.getTime())) return ts
    const fmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/Chicago',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZoneName: 'shortOffset',  // "GMT-5" / "GMT-6"
    })
    const parts: Record<string, string> = {}
    for (const p of fmt.formatToParts(d)) parts[p.type] = p.value
    // Normalize offset: "GMT-5" → "UTC-5".
    const offset = (parts.timeZoneName ?? 'GMT').replace('GMT', 'UTC')
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} ${offset}`
  } catch {
    return ts
  }
}


export function AutoPushPanel() {
  const [pushes, setPushes] = useState<PushSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Hidden by default — operator opts in via the header toggle.
  const [includeP3, setIncludeP3] = useState(false)
  // Per-card expansion state: id → full PushDetail (or 'loading').
  const [expanded, setExpanded] = useState<Record<number, PushDetail | 'loading'>>({})
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const setPushDetail = useSession((s) => s.setPushDetail)
  const containerRef = useRef<HTMLDivElement>(null)

  // Initial backfill + live SSE subscription.
  useEffect(() => {
    let cancelled = false
    fetchRecentPushes(2, { includeP3 })
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
  }, [includeP3])

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

  // Sort by tier ascending (P0 first), then ts DESC within each tier.
  // SSE may have prepended rows out of priority order; this re-sort
  // keeps the display consistent without round-tripping to the server.
  const sortedPushes = [...pushes].sort((a, b) => {
    const rankDiff = TIER_RANK[tierOf(a)] - TIER_RANK[tierOf(b)]
    if (rankDiff !== 0) return rankDiff
    return b.ts.localeCompare(a.ts)
  })
  // The frontend P3-hide filter is layered on top of the backend's
  // filter. With includeP3=true the backend ships P3 rows too; we
  // keep them all. With includeP3=false the backend already strips P3.
  const visiblePushes = sortedPushes

  return (
    <div className="flex-1 flex flex-col bg-white min-h-0">
      <div className="px-4 py-2 border-b border-slate-100 bg-slate-50 flex items-center justify-between gap-2">
        <div className="text-[11px] text-slate-500">{headerText}</div>
        <label className="text-[11px] text-slate-600 select-none cursor-pointer flex items-center gap-1">
          <input
            type="checkbox"
            className="h-3 w-3"
            checked={includeP3}
            onChange={(e) => setIncludeP3(e.target.checked)}
          />
          📋 显示低优先级 (P3)
        </label>
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
        {visiblePushes.map((p) => {
          const isSelected = selectedId === p.id
          const expansion = expanded[p.id]
          const isExpanded = expansion && expansion !== 'loading'
          const isLoadingThis = expansion === 'loading'
          const icon = AGENT_ICON[p.agent] ?? '🔔'
          const title = p.title ?? `${p.agent} push`
          const tier = tierOf(p)
          // P0 gets a thick rose border on the left so it stands out
          // in a scrolling feed.
          const tierBorder = tier === 'P0'
            ? ' border-l-4 border-l-rose-500'
            : ''
          return (
            <article
              key={p.id}
              className={
                'border rounded-lg p-4 space-y-3 cursor-pointer transition-all ' +
                (isSelected
                  ? 'bg-blue-50 border-blue-300 shadow-md'
                  : 'bg-white border-slate-200 shadow-sm hover:border-slate-300 hover:shadow-md') +
                tierBorder
              }
              onClick={() => onCardClick(p)}
              data-push-id={p.id}
              data-agent={p.agent}
              data-tier={tier}
            >
              <header className="flex items-baseline justify-between gap-2">
                <h3 className="text-sm font-medium text-ink-800 truncate flex items-baseline gap-2">
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-semibold mono shrink-0 ${TIER_CHIP_COLOR[tier]}`}
                  >
                    {tier}
                  </span>
                  <span className="mr-1.5">{icon}</span>
                  <span className="truncate">{title}</span>
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
