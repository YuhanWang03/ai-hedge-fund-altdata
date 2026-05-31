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

  reset: () => set({
    currentSessionId: null,
    currentIntent: null,
    currentCached: false,
    events: [],
    chat: [],
    highlightedStepId: null,
  }),
}))
