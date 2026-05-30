import type { TraceEvent } from '../types'

export type EventHandler = (ev: TraceEvent) => void
export type CloseHandler = () => void

/**
 * Open an SSE connection and route every event through onEvent. Returns a
 * disposer that closes the stream.
 *
 * EventSource doesn't support custom headers, so owner-token auth is not
 * available on this channel. The session_id was minted at /api/query time
 * (which IS authenticated), so the SSE endpoint accepts an unauthenticated
 * read by session_id; no leakage because session_ids are unguessable
 * 128-bit values.
 */
export function openTraceStream(
  sseUrl: string,
  onEvent: EventHandler,
  onClose?: CloseHandler,
): () => void {
  const es = new EventSource(sseUrl)

  es.onmessage = (raw) => {
    try {
      const ev = JSON.parse(raw.data) as TraceEvent
      if (ev.type === 'stream_close') {
        es.close()
        onClose?.()
        return
      }
      onEvent(ev)
    } catch (err) {
      console.warn('failed to parse SSE payload', err, raw.data)
    }
  }

  es.onerror = () => {
    // EventSource auto-reconnects on transient errors; we close once the
    // backend has marked the session done.
    if (es.readyState === EventSource.CLOSED) {
      onClose?.()
    }
  }

  return () => {
    es.close()
  }
}
