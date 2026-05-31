import { useEffect, useRef, useState } from 'react'
import { postQuery, QueryError } from '../../api/client'
import { openTraceStream } from '../../api/trace_sse'
import { useSession } from '../../store/session'

export function ChatPanel() {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const startSession = useSession((s) => s.startSession)
  const pushEvent = useSession((s) => s.pushEvent)
  const pushChat = useSession((s) => s.pushChat)
  const chat = useSession((s) => s.chat)
  const messagesEnd = useRef<HTMLDivElement>(null)

  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chat])

  const submit = async () => {
    const t = text.trim()
    if (!t || busy) return
    setBusy(true)
    setText('')
    try {
      const resp = await postQuery(t)
      startSession(resp.session_id, t, resp.intent ?? null, !!resp.cached)

      const close = openTraceStream(
        resp.sse_url,
        (ev) => pushEvent(ev),
        () => setBusy(false),
      )
      // safety: if stream lingers, time-out client-side
      setTimeout(() => {
        close()
        setBusy(false)
      }, 60_000)
    } catch (err) {
      const e = err as QueryError
      pushChat({
        id: `sys_${Date.now()}`,
        role: 'system',
        text: friendlyError(e),
        ts_ms: Date.now(),
      })
      setBusy(false)
    }
  }

  return (
    <div className="w-[30%] min-w-[360px] max-w-[520px] flex flex-col bg-white">
      <div className="px-4 py-3 border-b border-ink-200">
        <h2 className="text-sm font-semibold text-ink-800">Chat</h2>
        <div className="text-xs text-ink-500">
          自然语言 · 或试试「NVDA 为什么跌？」、「看看 AAPL 怎么样」
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        {chat.length === 0 && (
          <div className="text-ink-400 text-sm text-center mt-12">
            发条消息开始 →
          </div>
        )}
        {chat.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        <div ref={messagesEnd} />
      </div>

      <div className="border-t border-ink-200 px-3 py-3">
        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            placeholder="输入查询… (Enter 发送, Shift+Enter 换行)"
            rows={2}
            disabled={busy}
            className="flex-1 resize-none border border-ink-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-ink-400 disabled:bg-ink-50"
          />
          <button
            onClick={submit}
            disabled={busy || !text.trim()}
            className="bg-ink-800 text-white text-sm px-4 py-2 rounded-lg disabled:bg-ink-300"
          >
            {busy ? '…' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  )
}

function MessageBubble({ message: m }: { message: ReturnType<typeof useSession.getState>['chat'][number] }) {
  if (m.role === 'system') {
    return (
      <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mono">
        {m.text}
      </div>
    )
  }
  const isUser = m.role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={
          'max-w-[88%] rounded-2xl px-3 py-2 whitespace-pre-wrap text-sm break-words ' +
          (isUser
            ? 'bg-ink-800 text-white rounded-br-sm'
            : 'bg-ink-100 text-ink-800 rounded-bl-sm')
        }
      >
        {m.text}
        {m.cached && (
          <div className="mt-1 text-[10px] mono opacity-70">
            ✓ replay · cached
            {m.cachedAtMs
              ? ` ${new Date(m.cachedAtMs).toLocaleTimeString()}`
              : ''}
          </div>
        )}
      </div>
    </div>
  )
}

function friendlyError(e: QueryError): string {
  if (e.error === 'daily_budget_exhausted') {
    return `🛑 今日访客查询预算已用完，UTC 0:00 后重置（${e.resets_at_utc ?? ''}）。`
  }
  if (e.error === 'rate_limited') {
    const mins = e.retry_after_s ? Math.ceil(e.retry_after_s / 60) : 60
    return `⏱ 每 IP 每小时上限 5 次，约 ${mins} 分钟后可继续。`
  }
  if (e.error === 'intent_not_allowed_for_guest') {
    return `🔒 访客模式仅开放：异动归因、个股快照、产业链、13F、ETF、最近异动。`
  }
  return `❌ ${e.message}`
}
