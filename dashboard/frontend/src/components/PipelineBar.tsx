// Pipeline progress bar — pill row showing where the current query is
// in its intent-specific plan.
//
// Renders nothing for unknown intents. Pills become done as events
// flow through eventToStep(); the next not-yet-triggered pill pulses.
// Clicking a done/active pill scrolls the trace pane to its first
// matching event.

import { useMemo } from 'react'
import { eventToStep, isSessionComplete } from '../event_to_step'
import { getPipeline, STEP_DEFS } from '../pipelines'
import type { TraceEvent } from '../types'

interface Props {
  intent: string | null
  events: TraceEvent[]
  cached?: boolean
  // Called when the user clicks a done/active pill after the session has
  // finished. Lets the parent attach a highlight ring to matching events.
  onPillActivate?: (stepId: string) => void
  // ID of the currently highlighted step (driven by the click handler).
  // The matching pill itself gets the glass ring so it's clear which one
  // is "active" in the highlight sense.
  highlightedStepId?: string | null
}

type State = 'pending' | 'active' | 'done' | 'error'

export function PipelineBar({ intent, events, cached, onPillActivate, highlightedStepId }: Props) {
  const complete = isSessionComplete(events, !!cached)
  const pipeline = useMemo(() => getPipeline(intent), [intent])
  const { states, firstEventBySeq } = useMemo(() => {
    if (!pipeline) return { states: {}, firstEventBySeq: {} as Record<string, number> }

    // Find the first event seq for each step (used for scroll-on-click).
    const firstSeqByStep: Record<string, number> = {}
    let maxTriggeredIdx = -1
    let sawErrorStep: string | null = null

    for (const ev of events) {
      const step = eventToStep(ev, intent ?? undefined)
      if (step && pipeline.includes(step) && firstSeqByStep[step] === undefined) {
        firstSeqByStep[step] = typeof ev.seq === 'number' ? ev.seq : 0
      }
      if (step && pipeline.includes(step)) {
        const idx = pipeline.indexOf(step)
        if (idx > maxTriggeredIdx) maxTriggeredIdx = idx
      }
      if (ev.type === 'error') {
        // Attribute the error to the most recently-triggered step.
        sawErrorStep = pipeline[Math.max(0, maxTriggeredIdx)]
      }
    }

    const sessionEnded = events.some(e => e.type === 'session_end')
    const states: Record<string, State> = {}
    for (let i = 0; i < pipeline.length; i++) {
      const stepId = pipeline[i]
      if (cached) {
        states[stepId] = 'done'
        continue
      }
      if (stepId === sawErrorStep) {
        states[stepId] = 'error'
        continue
      }
      if (i <= maxTriggeredIdx) {
        states[stepId] = 'done'
      } else if (i === maxTriggeredIdx + 1 && !sessionEnded) {
        states[stepId] = 'active'
      } else {
        states[stepId] = 'pending'
      }
    }

    return { states, firstEventBySeq: firstSeqByStep }
  }, [pipeline, events, cached])

  if (!pipeline) return null

  const onClickStep = (stepId: string) => {
    const seq = firstEventBySeq[stepId]
    if (seq !== undefined) {
      const el = document.querySelector(`[data-event-seq="${seq}"]`)
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
    // Highlight only after the session is done; mid-stream we keep it
    // distraction-free and only scroll.
    if (complete && onPillActivate) onPillActivate(stepId)
  }

  return (
    <div className="flex items-center gap-1 px-4 py-3 overflow-x-auto">
      {pipeline.map((stepId, idx) => (
        <span key={stepId} className="flex items-center gap-1 shrink-0">
          <StepPill
            stepId={stepId}
            state={states[stepId] ?? 'pending'}
            highlighted={highlightedStepId === stepId}
            onClick={() => onClickStep(stepId)}
          />
          {idx < pipeline.length - 1 && <Arrow />}
        </span>
      ))}
      {cached && <ReplayChip />}
    </div>
  )
}

function StepPill({
  stepId, state, onClick, highlighted,
}: { stepId: string; state: State; onClick: () => void; highlighted: boolean }) {
  const def = STEP_DEFS[stepId] ?? { icon: '·', label: stepId }
  const colorByState: Record<State, string> = {
    pending: 'bg-white border-slate-200 text-slate-400',
    active:  'bg-blue-500 border-blue-500 text-white ring-2 ring-blue-200 animate-pulse',
    done:    'bg-emerald-500 border-emerald-500 text-white',
    error:   'bg-rose-500 border-rose-500 text-white',
  }
  const interactive = state === 'done' || state === 'active' || state === 'error'
  return (
    <button
      type="button"
      onClick={interactive ? onClick : undefined}
      disabled={!interactive}
      title={state === 'error' ? `⚠️ ${def.label} 出错` : def.label}
      className="flex flex-col items-center group focus:outline-none"
    >
      <div
        className={
          'w-8 h-8 rounded-full border flex items-center justify-center text-sm leading-none ' +
          'transition-all duration-300 ' +
          colorByState[state] +
          (interactive ? ' cursor-pointer group-hover:scale-110' : ' cursor-default') +
          (highlighted ? ' pill-glass-highlight' : '')
        }
      >
        <span>{def.icon}</span>
      </div>
      <span className="mt-1 text-[10px] leading-none text-slate-600">{def.label}</span>
    </button>
  )
}

function Arrow() {
  return (
    <span className="text-slate-300 text-xs select-none -mt-3" aria-hidden>→</span>
  )
}

function ReplayChip() {
  return (
    <span className="ml-3 text-[10px] mono px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 border border-amber-200">
      Replay
    </span>
  )
}
