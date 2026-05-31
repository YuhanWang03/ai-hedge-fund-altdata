import { create } from 'zustand'
import type { ChatMessage, TraceEvent } from '../types'

/**
 * What a "user chat" view holds: the trace + chat for the latest
 * interactive query the user typed. Owns chat history.
 */
interface UserChatView {
  currentSessionId: string | null
  currentIntent: string | null
  currentCached: boolean
  events: TraceEvent[]
  chat: ChatMessage[]
}

/**
 * What a "push detail" view holds: the trace + reply text for an
 * auto-push card the user clicked in AutoPushPanel. Independent of
 * the chat history.
 */
export interface PushDetailView {
  pushId: number
  intent: string | null
  events: TraceEvent[]
  push: {
    ts: string
    agent: string
    title: string | null
    text_html: string | null
    tickers: string | null
  }
}

interface SessionState {
  // User-chat view (default mode). Owned by ChatPanel; TracePanel reads
  // from here when chatMode === 'qa'.
  currentSessionId: string | null
  currentIntent: string | null
  currentCached: boolean
  events: TraceEvent[]
  chat: ChatMessage[]

  // Push-detail view (auto_push mode). When non-null, TracePanel reads
  // events/intent from here instead of from the user-chat fields.
  // ChatPanel NEVER reads this — auto-push clicks don't pollute the
  // user's conversation history.
  pushDetail: PushDetailView | null

  // Active pipeline-pill highlight (shared across both views).
  highlightedStepId: string | null

  startSession: (id: string, userText: string, intent: string | null, cached: boolean) => void
  pushEvent: (ev: TraceEvent) => void
  pushChat: (msg: ChatMessage) => void
  markChatCached: (sessionId: string, cachedAtMs: number) => void
  setHighlightedStepId: (stepId: string | null) => void
  /**
   * Set the push-detail view (auto-push card click). Replaces any prior
   * pushDetail. Pass null to clear.
   */
  setPushDetail: (detail: PushDetailView | null) => void
  reset: () => void
}

export const useSession = create<SessionState>((set) => ({
  currentSessionId: null,
  currentIntent: null,
  currentCached: false,
  events: [],
  chat: [],
  pushDetail: null,
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

  setPushDetail: (detail) =>
    // Setting/clearing the push-detail view leaves the user-chat fields
    // and the chat history untouched. We do reset highlightedStepId so
    // a click on one push doesn't leave a glass ring from another.
    set({ pushDetail: detail, highlightedStepId: null }),

  reset: () => set({
    currentSessionId: null,
    currentIntent: null,
    currentCached: false,
    events: [],
    chat: [],
    pushDetail: null,
    highlightedStepId: null,
  }),
}))
