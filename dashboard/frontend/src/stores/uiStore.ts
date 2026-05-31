// UI-only client state. Separate from the session store so it survives
// page navigation without dragging the trace history along.

import { create } from 'zustand'

export type ChatMode = 'auto_push' | 'qa'

interface UIState {
  chatMode: ChatMode
  setChatMode: (m: ChatMode) => void
}

// localStorage is unavailable during SSR (the render_check.mjs script
// uses react-dom/server). Guard the initial read.
function readInitialChatMode(): ChatMode {
  if (typeof window === 'undefined') return 'qa'
  const stored = window.localStorage.getItem('chatMode')
  return stored === 'auto_push' || stored === 'qa' ? stored : 'qa'
}

export const useUIStore = create<UIState>((set) => ({
  chatMode: readInitialChatMode(),
  setChatMode: (m) => {
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.setItem('chatMode', m)
      } catch {
        /* private mode, etc. — ignore */
      }
    }
    set({ chatMode: m })
  },
}))
