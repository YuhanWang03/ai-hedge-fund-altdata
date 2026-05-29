import { BudgetBanner } from './components/BudgetBanner'
import { ChatPanel } from './components/ChatPanel/ChatPanel'
import { TracePanel } from './components/TracePanel/TracePanel'

export default function App() {
  return (
    <div className="h-screen w-screen flex flex-col bg-ink-50 text-ink-800">
      <BudgetBanner />
      <div className="flex-1 flex overflow-hidden">
        <TracePanel />
        <ChatPanel />
      </div>
    </div>
  )
}
