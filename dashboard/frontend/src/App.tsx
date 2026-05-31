import { useEffect, useState } from 'react'
import { getOwnerToken } from './api/client'
import { AutoPushPanel } from './components/AutoPushPanel'
import { BudgetBanner } from './components/BudgetBanner'
import { ChatModeToggle } from './components/ChatModeToggle'
import { ChatPanel } from './components/ChatPanel/ChatPanel'
import { TracePanel } from './components/TracePanel/TracePanel'
import { useUIStore } from './stores/uiStore'

export default function App() {
  // Owner status is gated by the presence of a token in localStorage.
  // The server still enforces auth — this only decides whether to show
  // the auto-push toggle. A wrong token surfaces via 401 elsewhere.
  const [isOwner, setIsOwner] = useState<boolean>(() => !!getOwnerToken())

  // Re-check on focus so logging in from another tab takes effect.
  useEffect(() => {
    const onFocus = () => setIsOwner(!!getOwnerToken())
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [])

  const chatMode = useUIStore((s) => s.chatMode)
  const setChatMode = useUIStore((s) => s.setChatMode)
  // Guests never see the toggle; they always get the chat panel.
  const effectiveMode = isOwner ? chatMode : 'qa'

  return (
    <div className="h-screen w-screen flex flex-col bg-ink-50 text-ink-800">
      <BudgetBanner />
      <div className="flex-1 flex overflow-hidden">
        <TracePanel />
        <div className="w-[30%] min-w-[360px] max-w-[520px] flex flex-col bg-white border-l border-ink-200">
          {isOwner && (
            <ChatModeToggle mode={chatMode} onChange={setChatMode} />
          )}
          {effectiveMode === 'auto_push' ? <AutoPushPanel /> : <ChatPanel />}
        </div>
      </div>
    </div>
  )
}
