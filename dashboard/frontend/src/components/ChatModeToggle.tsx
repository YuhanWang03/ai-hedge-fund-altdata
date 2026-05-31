// Segmented control that swaps the right pane between the read-only
// auto-push feed (📨) and the interactive chat input (💬). Owner-only.

import type { ChatMode } from '../stores/uiStore'

interface Props {
  mode: ChatMode
  onChange: (m: ChatMode) => void
}

const SEGMENTS: { mode: ChatMode; label: string }[] = [
  { mode: 'auto_push', label: '📨 自动推送' },
  { mode: 'qa',        label: '💬 用户问答' },
]

export function ChatModeToggle({ mode, onChange }: Props) {
  return (
    <div className="sticky top-0 z-20 bg-white border-b border-slate-200 px-3 py-2">
      <div className="inline-flex items-center bg-slate-100 rounded-lg p-1 gap-1 w-full">
        {SEGMENTS.map((seg) => {
          const active = seg.mode === mode
          return (
            <button
              key={seg.mode}
              type="button"
              onClick={() => onChange(seg.mode)}
              className={
                'flex-1 h-8 px-3 rounded-md text-xs font-medium select-none ' +
                'transition-colors duration-200 ' +
                (active
                  ? 'bg-blue-500 text-white shadow-sm'
                  : 'bg-transparent text-slate-600 hover:text-slate-900')
              }
            >
              {seg.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}
