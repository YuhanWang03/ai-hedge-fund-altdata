// Extracted from TracePanel so render_check can SSR-test the conditional
// behavior without fighting zustand's SSR snapshot semantics.
//
// Two flavors:
//   1. showingPushDetail=true  → "this push predates the trace mechanism"
//   2. showingPushDetail=false → generic dashboard onboarding hint

interface Props {
  showingPushDetail: boolean
  pushTs: string | null
}

export function EmptyState({ showingPushDetail, pushTs }: Props) {
  if (showingPushDetail) {
    const dateOnly = (pushTs ?? '').slice(0, 10)
    return (
      <div className="text-slate-500 text-sm p-6 leading-relaxed">
        📅 此推送发出于 trace 捕获机制上线之前
        （{dateOnly}），无 trace 数据。
        <br />
        <br />
        点击 <code className="mono text-slate-700">2026-05-31</code> 之后的新推送可查看完整 trace。
      </div>
    )
  }
  return (
    <div className="text-ink-400 text-sm text-center mt-20">
      发起一次查询后，每个模块调用、LLM prompt、API call、DB
      写入都会出现在这里。
    </div>
  )
}
