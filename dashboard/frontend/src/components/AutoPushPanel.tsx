// Read-only feed of recent automated pushes (scheduler + streamer agents).
// Phase 1: hard-coded mock data, no backend wiring. Phase 2 will replace
// MOCK_PUSHES with a fetch from a new /api/pushes endpoint and the
// "查看完整 Trace" button will navigate to a replay view.

import { useState } from 'react'

interface PushRecord {
  id: number
  agent: string
  icon: string
  title: string
  timestamp: string  // already formatted as "YYYY-MM-DD HH:MM ET"
  body: string
}

const MOCK_PUSHES: PushRecord[] = [
  {
    id: 1,
    agent: 'anomaly',
    icon: '⚡',
    title: '盘中异动 · IBM',
    timestamp: '2026-05-29 10:15 ET',
    body: '📈 现价 $297.97  +7.45%\n📊 成交量 28.4M · 节奏 3.6× · 已交易 11%\n★ 逆势上涨（XLK +0.40%）',
  },
  {
    id: 2,
    agent: 'institutional',
    icon: '🏛',
    title: 'Berkshire 13F · 2026-Q1',
    timestamp: '2026-05-29 18:00 ET',
    body: '新进 DAL $2.6B · 大幅加仓 GOOGL → 5.9%\n清仓 V / MA / DPZ / UNH',
  },
  {
    id: 3,
    agent: 'screen',
    icon: '📋',
    title: '科技股筛选 · 7 candidates',
    timestamp: '2026-05-29 17:30 ET',
    body: 'NVDA / META / GOOGL / AMD / AVGO / CRWD / PANW 通过筛选',
  },
  {
    id: 4,
    agent: 'etf',
    icon: '📈',
    title: 'ARK 每日持仓',
    timestamp: '2026-05-29 17:00 ET',
    body: 'ARKK / ARKW / ARKG / ARKF 当日 snapshot 已入库',
  },
  {
    id: 5,
    agent: 'lateral',
    icon: '🕸',
    title: '产业链横向扩展 · AMD',
    timestamp: '2026-05-29 18:00 ET',
    body: 'Tavily 验证通过 4 个邻居：CRWD / NET / DDOG / PANW',
  },
  {
    id: 6,
    agent: 'anomaly',
    icon: '⚡',
    title: '盘中异动 · CRM',
    timestamp: '2026-05-29 14:32 ET',
    body: '📈 +6.04%  节奏 2.6×  对比 XLK +5.13pp ★ 逆势',
  },
]

// Sort newest first. Timestamp strings sort lexicographically because
// they're "YYYY-MM-DD HH:MM ET".
const SORTED_PUSHES = [...MOCK_PUSHES].sort((a, b) => b.timestamp.localeCompare(a.timestamp))


export function AutoPushPanel() {
  const [toast, setToast] = useState<string | null>(null)

  const showToast = (msg: string) => {
    setToast(msg)
    window.setTimeout(() => setToast(null), 2400)
  }

  return (
    <div className="flex-1 flex flex-col bg-white min-h-0">
      <div className="px-4 py-2 border-b border-slate-100 bg-slate-50">
        <div className="text-[11px] text-slate-500">
          📡 最近 2 个交易日推送 · 数据每日 0:00 UTC 清理
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3">
        {SORTED_PUSHES.length === 0 && (
          <div className="text-slate-400 text-sm text-center mt-12">暂无推送</div>
        )}
        {SORTED_PUSHES.map((p) => (
          <article
            key={p.id}
            className="bg-white border border-slate-200 rounded-lg p-4 space-y-3 shadow-sm"
            data-push-id={p.id}
            data-agent={p.agent}
          >
            <header className="flex items-baseline justify-between gap-2">
              <h3 className="text-sm font-medium text-ink-800">
                <span className="mr-1.5">{p.icon}</span>
                {p.title}
              </h3>
              <span className="text-[11px] text-slate-500 mono shrink-0">{p.timestamp}</span>
            </header>
            <div className="border-t border-slate-100" />
            <div className="text-[13px] text-slate-700 whitespace-pre-wrap leading-relaxed">
              {p.body}
            </div>
            <button
              type="button"
              onClick={() => showToast('Phase 2 即将上线：trace 回放')}
              className="text-xs text-slate-500 bg-slate-100 hover:bg-slate-200 px-3 py-1.5 rounded transition-colors"
            >
              点击查看完整 Trace
            </button>
          </article>
        ))}
      </div>

      {toast && (
        <div className="fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg bg-ink-800 text-white text-sm shadow-lg">
          {toast}
        </div>
      )}
    </div>
  )
}
