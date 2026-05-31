import { create } from 'zustand'
import type { ChatMessage, TraceEvent } from '../types'

interface SessionState {
  // Currently displayed session (most recent).
  currentSessionId: string | null
  currentIntent: string | null
  currentCached: boolean
  events: TraceEvent[]
  chat: ChatMessage[]

  // Active pipeline-pill highlight, or null when nothing is highlighted.
  // Only takes effect after the session is complete (see isSessionComplete).
  highlightedStepId: string | null

  startSession: (id: string, userText: string, intent: string | null, cached: boolean) => void
  pushEvent: (ev: TraceEvent) => void
  pushChat: (msg: ChatMessage) => void
  markChatCached: (sessionId: string, cachedAtMs: number) => void
  setHighlightedStepId: (stepId: string | null) => void
  /**
   * Replay a fully-archived push into the panels. Used by AutoPushPanel
   * when the user clicks a feed card: fetches the full trace + text and
   * dumps it into the session state so TracePanel + ChatPanel both
   * re-render against it.
   */
  loadPush: (args: {
    sessionId: string
    intent: string | null
    label: string                  // shown as the "user text" header
    events: TraceEvent[]
    replyText: string | null
    ts_ms: number
  }) => void
  reset: () => void
}

export const useSession = create<SessionState>((set) => ({
  currentSessionId: null,
  currentIntent: null,
  currentCached: false,
  events: [],
  chat: [],
  highlightedStepId: null,

  startSession: (id, userText, intent, cached) =>
    set((s) => ({
      currentSessionId: id,
      currentIntent: intent,
      currentCached: cached,
      // Keep prior events as history? No — wipe trace per query.
      events: [],
      // Clear any pill highlight left over from the previous query.
      highlightedStepId: null,
      chat: [
        ...s.chat,
        {
          id: `u_${id}`,
          role: 'user',
          text: userText,
          ts_ms: Date.now(),
          sessionId: id,
        },
      ],
    })),

  pushEvent: (ev) =>
    set((s) => {
      const events = [...s.events, ev]
      // When the bot's chat_message event arrives, also project it into
      // the chat panel so the conversation stays in sync.
      if (ev.type === 'chat_message' && typeof ev.text === 'string') {
        return {
          events,
          chat: [
            ...s.chat,
            {
              id: `b_${ev.session_id}_${ev.seq ?? 0}`,
              role: 'bot',
              text: ev.text,
              ts_ms: ev.ts_ms ?? Date.now(),
              sessionId: ev.session_id,
              cached: !!ev.replayed,
              cachedAtMs:
                typeof ev.cached_at_ms === 'number' ? ev.cached_at_ms : undefined,
            },
          ],
        }
      }
      return { events }
    }),

  pushChat: (msg) => set((s) => ({ chat: [...s.chat, msg] })),

  markChatCached: (sessionId, cachedAtMs) =>
    set((s) => ({
      chat: s.chat.map((m) =>
        m.sessionId === sessionId && m.role === 'bot'
          ? { ...m, cached: true, cachedAtMs }
          : m,
      ),
    })),

  setHighlightedStepId: (stepId) => set({ highlightedStepId: stepId }),

  loadPush: ({ sessionId, intent, label, events, replyText, ts_ms }) =>
    set(() => {
      // Build a synthetic chat: a "user message" carrying the push title
      // + a "bot message" carrying the full HTML reply. Replace any
      // prior chat — feed clicks are independent of the chat history.
      const chat: ChatMessage[] = [
        {
          id: `push_label_${sessionId}`,
          role: 'user',
          text: label,
          ts_ms,
          sessionId,
        },
      ]
      if (replyText) {
        chat.push({
          id: `push_reply_${sessionId}`,
          role: 'bot',
          text: replyText,
          ts_ms,
          sessionId,
          cached: true,
          cachedAtMs: ts_ms,
        })
      }
      return {
        currentSessionId: sessionId,
        currentIntent: intent,
        currentCached: true,    // archived runs are by definition "replay"
        events,
        chat,
        highlightedStepId: null,
      }
    }),

  reset: () => set({
    currentSessionId: null,
    currentIntent: null,
    currentCached: false,
    events: [],
    chat: [],
    highlightedStepId: null,
  }),
}))
