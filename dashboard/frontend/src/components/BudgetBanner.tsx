import { useEffect, useState } from 'react'
import { fetchBudgetStatus, getOwnerToken, setOwnerToken } from '../api/client'
import type { BudgetStatus } from '../types'

export function BudgetBanner() {
  const [status, setStatus] = useState<BudgetStatus | null>(null)
  const [tokenInput, setTokenInput] = useState('')
  const [showTokenField, setShowTokenField] = useState(false)

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const s = await fetchBudgetStatus()
        if (!cancelled) setStatus(s)
      } catch {
        /* ignore */
      }
    }
    tick()
    const id = setInterval(tick, 5000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const isOwner = status?.kind === 'owner'

  return (
    <div className="flex items-center justify-between px-4 py-2 border-b border-ink-200 bg-white">
      <div className="flex items-center gap-3">
        <div className="text-sm font-semibold tracking-tight text-ink-800">
          AI Hedge Fund · Live Trace
        </div>
        <span
          className={
            'text-xs px-2 py-0.5 rounded-full ' +
            (isOwner
              ? 'bg-ink-800 text-white'
              : 'bg-ink-100 text-ink-700 border border-ink-200')
          }
        >
          {isOwner ? 'OWNER' : 'GUEST'}
        </span>
      </div>

      <div className="flex items-center gap-4 text-xs text-ink-600 mono">
        {status && !isOwner && (
          <>
            <span>
              今日预算{' '}
              <span className="text-ink-800 font-medium">
                ${status.global_daily_remaining_usd.toFixed(3)}
              </span>{' '}
              / ${status.global_daily_cap_usd.toFixed(2)}
            </span>
            {typeof status.your_ip_hourly_remaining === 'number' && (
              <span>
                本小时剩余{' '}
                <span className="text-ink-800 font-medium">
                  {status.your_ip_hourly_remaining}
                </span>
                /5 次
              </span>
            )}
          </>
        )}
        {!showTokenField && (
          <button
            className="text-ink-500 hover:text-ink-800"
            onClick={() => setShowTokenField(true)}
          >
            {getOwnerToken() ? 'change token' : 'owner login'}
          </button>
        )}
        {showTokenField && (
          <span className="flex items-center gap-2">
            <input
              autoFocus
              type="password"
              className="border border-ink-200 rounded px-2 py-1 text-xs w-40"
              placeholder="X-Owner-Token"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
            />
            <button
              className="bg-ink-800 text-white text-xs px-2 py-1 rounded"
              onClick={() => {
                setOwnerToken(tokenInput || null)
                setShowTokenField(false)
                setTokenInput('')
                window.location.reload()
              }}
            >
              save
            </button>
            <button
              className="text-ink-500 text-xs"
              onClick={() => {
                setOwnerToken(null)
                setShowTokenField(false)
                setTokenInput('')
                window.location.reload()
              }}
            >
              clear
            </button>
          </span>
        )}
      </div>
    </div>
  )
}
