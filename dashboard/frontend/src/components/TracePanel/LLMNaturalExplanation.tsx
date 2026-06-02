// Natural-language event explainer. Owner-only, qa-mode-only.
// Fires its fetch on mount and stays mounted as long as the user
// keeps the parent <details> block of TraceEvent open.
//
// First call to a given event shape spends ~$0.0006 of DeepSeek tokens;
// subsequent calls in the same browser session hit the server's LRU
// cache and return instantly.

import { useEffect, useState } from 'react'
import { fetchEventExplanation } from '../../api/explain_event'
import type { TraceEvent } from '../../types'

interface Props {
  event: TraceEvent
  intent: string | null
}

export function LLMNaturalExplanation({ event, intent }: Props) {
  const [text, setText] = useState<string | null>(null)
  const [errored, setErrored] = useState(false)

  useEffect(() => {
    let cancelled = false
    setText(null)
    setErrored(false)
    // Strip the dashboard-attached fields the cache key already ignores
    // so we don't pay for sending them on the wire.
    const evPayload = { ...event } as Record<string, unknown>
    delete evPayload.explanation
    delete evPayload.session_id
    delete evPayload.replayed
    delete evPayload.cached_from
    delete evPayload.cached_at_ms

    fetchEventExplanation(evPayload, intent)
      .then((resp) => {
        if (!cancelled) setText(resp.explanation)
      })
      .catch(() => {
        if (!cancelled) setErrored(true)
      })

    return () => {
      cancelled = true
    }
  }, [event.session_id, event.seq, intent])

  // Silently degrade on error — the static 5-field 📖 解析 above is
  // still useful, no need to clutter the UI with an error chip.
  if (errored) return null

  return (
    <div className="mt-2 pl-3 border-l-2 border-blue-300 bg-slate-50 rounded-r py-1.5 pr-2">
      <div className="text-[11px] text-slate-500 mb-0.5">💬 通俗解析</div>
      {text === null ? (
        <div className="text-[13px] text-slate-400 italic">
          🤖 通俗解析加载中…
        </div>
      ) : (
        <div className="text-[13px] text-slate-700 leading-relaxed whitespace-pre-wrap">
          {text}
        </div>
      )}
    </div>
  )
}
