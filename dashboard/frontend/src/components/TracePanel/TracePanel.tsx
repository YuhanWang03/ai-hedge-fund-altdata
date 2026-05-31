import { useEffect, useRef, useState } from 'react'
import { PipelineBar } from '../PipelineBar'
import { intentLabel } from '../../pipelines'
import { isSessionComplete, shouldHighlight } from '../../event_to_step'
import { useSession } from '../../store/session'
import { TraceEvent } from './TraceEvent'

export function TracePanel() {
  const events = useSession((s) => s.events)
  const intent = useSession((s) => s.currentIntent)
  const cached = useSession((s) => s.currentCached)
  const highlightedStepId = useSession((s) => s.highlightedStepId)
  const setHighlightedStepId = useSession((s) => s.setHighlightedStepId)
  const containerRef = useRef<HTMLDivElement>(null)

  const sessionComplete = isSessionComplete(events, cached)

  // Global "expand all explanations" state. `forceOpen=null` means each
  // <details> uses its own toggle state. Clicking the button flips
  // forceOpen and bumps the key so React remounts each <details> with the
  // new initial `open` value.
  const [forceOpen, setForceOpen] = useState<boolean | null>(null)
  const [bump, setBump] = useState(0)
  const toggleAll = () => {
    setForceOpen((prev) => (prev === true ? false : true))
    setBump((k) => k + 1)
  }
  const hasExplanations = events.some((e) => !!e.explanation)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [events])

  const startEvent = events.find((e) => e.type === 'session_start')
  const endEvent = events.find((e) => e.type === 'session_end')
  const intentEvent = events.find((e) => e.type === 'intent_classified')
  const userText = typeof startEvent?.text === 'string' ? startEvent.text : ''
  const isReplayed = events.some((e) => e.replayed)

  const llmCount = events.filter((e) => e.type === 'llm_call').length
  const apiCount = events.filter((e) => e.type === 'api_call').length
  const dbCount = events.filter((e) => e.type === 'db_write').length
  const totalCost = events
    .map((e) => (typeof e.cost_usd === 'number' ? e.cost_usd : 0))
    .reduce((a, b) => a + b, 0)

  return (
    <div className="flex-1 flex flex-col bg-ink-50 border-r border-ink-200 min-w-0">
      {/* Header */}
      <div className="px-5 py-3 border-b border-ink-200 bg-white">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 min-w-0">
            <h2 className="text-sm font-semibold text-ink-800 shrink-0">
              Execution Trace
            </h2>
            {userText && (
              <span
                className="text-sm text-ink-700 truncate max-w-[40ch]"
                title={userText}
              >
                「{userText}」
              </span>
            )}
            {isReplayed && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 border border-amber-200 mono shrink-0">
                Replay · cached
              </span>
            )}
            {intentEvent && (() => {
              const rawIntent = String(intentEvent.intent ?? '')
              const label = intentLabel(rawIntent)
              if (!label) return null
              return (
                <span
                  className="text-xs px-2 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200 shrink-0"
                  title={`intent: ${rawIntent}`}
                >
                  🎯 {label}
                </span>
              )
            })()}
          </div>
          <div className="flex items-center gap-4 text-xs mono text-ink-500">
            {hasExplanations && (
              <button
                onClick={toggleAll}
                className="text-slate-600 hover:text-slate-900 px-2 py-0.5 rounded border border-slate-200 bg-white hover:bg-slate-50"
                title="批量展开/收起每个事件下方的「📖 解析」"
              >
                {forceOpen === true ? '📖 收起所有解析' : '📖 展开所有解析'}
              </button>
            )}
            <span>API <span className="text-ink-800">{apiCount}</span></span>
            <span>LLM <span className="text-ink-800">{llmCount}</span></span>
            <span>DB <span className="text-ink-800">{dbCount}</span></span>
            <span>
              Cost <span className="text-ink-800">${totalCost.toFixed(5)}</span>
            </span>
          </div>
        </div>
      </div>

      {/* Pipeline progress (sticky beneath the header) */}
      {intent && (
        <div className="sticky top-0 z-10 bg-white border-b border-slate-200">
          <PipelineBar
            intent={intent}
            events={events}
            cached={cached}
            onPillActivate={setHighlightedStepId}
            highlightedStepId={highlightedStepId}
          />
        </div>
      )}

      {/* Stream */}
      <div
        ref={containerRef}
        onClick={(e) => {
          // Click anywhere outside an event row clears the highlight.
          if (e.target === e.currentTarget) setHighlightedStepId(null)
        }}
        className="flex-1 overflow-y-auto px-5 py-4 space-y-12"
      >
        {events.length === 0 && (
          <div className="text-ink-400 text-sm text-center mt-20">
            发起一次查询后，每个模块调用、LLM prompt、API call、DB
            写入都会出现在这里。
          </div>
        )}
        {events.map((ev, i) => (
          <TraceEvent
            key={`${ev.session_id}_${ev.seq ?? i}`}
            event={ev}
            forceExplanationOpen={forceOpen}
            explanationBump={bump}
            highlighted={shouldHighlight(ev, highlightedStepId, sessionComplete, intent ?? undefined)}
          />
        ))}
        {startEvent && !endEvent && (
          <div className="text-xs mono text-ink-400 animate-pulse">
            ▸ running…
          </div>
        )}
      </div>
    </div>
  )
}
