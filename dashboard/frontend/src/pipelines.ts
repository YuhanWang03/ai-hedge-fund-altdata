// Static pipeline definitions per intent. Each intent maps to an ordered
// list of step IDs; STEP_DEFS holds the visual metadata for each step.
// Step IDs are the contract between pipelines (this file) and the
// event-to-step mapper (event_to_step.ts).
//
// Concurrency note:
//
// The pipeline bar assumes steps execute as a linear time sequence. Today
// the v2/ codebase runs the explain_move / thirteen_f / etc. paths
// strictly sequentially, so the assumption holds.
//
// If a future change introduces concurrency (e.g. chain verifying N
// neighbor relations in parallel, or explain_move firing FD + Tavily
// fetches in parallel), the options are:
//
//   Option A (recommended): keep this linear bar. Multiple concurrent
//     events mapping to the same pill complete one at a time; the pill
//     transitions active → done once any of them finishes. Other
//     concurrent events on the same step are still visible in the
//     trace stream below.
//
//   Option B: render the pipeline as a fork/join tree. Only worth the
//     extra complexity if a concurrent branch is wide (>= 3 parallel
//     children) and shows up on a frequently-used path.
//
// No special handling needed for the current code base.

export interface StepDef {
  icon: string
  label: string
}

export const STEP_DEFS: Record<string, StepDef> = {
  input:         { icon: '📥', label: '输入'   },
  classify:      { icon: '🎯', label: '意图'   },
  edgar:         { icon: '📡', label: 'EDGAR'  },
  ark:           { icon: '📡', label: 'ARK'    },
  alpaca:        { icon: '📡', label: 'Alpaca' },
  price:         { icon: '💹', label: '行情'   },
  fundamentals:  { icon: '📊', label: '财务'   },
  insider:       { icon: '👥', label: '内部人' },
  news:          { icon: '📰', label: '新闻'   },
  aggregate:     { icon: '🔄', label: '聚合'   },
  detect:        { icon: '⚖️', label: '对比'   },
  llm_interpret: { icon: '🧠', label: '解读'   },
  llm_propose:   { icon: '🧩', label: '提议'   },
  verify:        { icon: '🛡️', label: '评级'   },
  generate:      { icon: '✍️', label: '归因'   },
  filter:        { icon: '🔍', label: '筛选'   },
  memory:        { icon: '🧠', label: '记忆'   },
  sqlite_read:   { icon: '💾', label: '查询'   },
  sqlite_write:  { icon: '💾', label: '写入'   },
  validate:      { icon: '✅', label: '校验'   },
  render:        { icon: '🎨', label: '卡片'   },
  reply:         { icon: '✉️', label: '回复'   },
}

export const PIPELINES: Record<string, string[]> = {
  thirteen_f:       ['input', 'classify', 'edgar', 'aggregate', 'detect', 'llm_interpret', 'render', 'reply'],
  // Confirmed against v2/monitoring/attributor.py:attribute():
  // _search_news → _entity_filter → _synthesize (Generator) → _verify_reasons
  // (Verifier) → memory.remember. Price is fetched upstream by the responder;
  // insider trades are NOT called on this path (it's purely news-based).
  explain_move:     ['input', 'classify', 'price', 'news', 'generate', 'verify', 'memory', 'render', 'reply'],
  summary:          ['input', 'classify', 'price', 'fundamentals', 'insider', 'news', 'llm_interpret', 'render', 'reply'],
  chain:            ['input', 'classify', 'price', 'llm_propose', 'news', 'filter', 'render', 'reply'],
  // sqlite_write captures save_snapshot, which fires on both the cron
  // path (Mon-Fri 17:00 ET ingest) and the on-demand /etf bot path.
  etf_view:         ['input', 'classify', 'ark', 'detect', 'sqlite_write', 'render', 'reply'],
  holders_view:     ['input', 'classify', 'sqlite_read', 'render', 'reply'],
  find_anomalies:   ['input', 'classify', 'sqlite_read', 'render', 'reply'],
  watchlist_view:   ['input', 'classify', 'sqlite_read', 'render', 'reply'],
  watchlist_add:    ['input', 'classify', 'validate', 'sqlite_write', 'render', 'reply'],
  watchlist_remove: ['input', 'classify', 'sqlite_write', 'render', 'reply'],
  alert_set:        ['input', 'classify', 'validate', 'sqlite_write', 'render', 'reply'],
  alert_list:       ['input', 'classify', 'sqlite_read', 'render', 'reply'],
  portfolio_view:   ['input', 'classify', 'alpaca', 'render', 'reply'],
  pnl_view:         ['input', 'classify', 'alpaca', 'render', 'reply'],
  settings:         ['input', 'classify', 'sqlite_read', 'render', 'reply'],
}

export function getPipeline(intent: string | null | undefined): string[] | null {
  if (!intent || intent === 'unknown') return null
  return PIPELINES[intent] ?? null
}


// Human-readable Chinese labels for each intent, surfaced in the trace
// header so non-technical visitors aren't shown raw enum names.
// The English intent stays available via tooltip for debugging.
export const INTENT_LABELS: Record<string, string> = {
  explain_move:     '异动归因',
  thirteen_f:       '机构持仓',
  summary:          '个股概览',
  chain:            '产业链',
  etf_view:         'ETF 持仓',
  holders_view:     '反查持仓',
  find_anomalies:   '最近异动',
  watchlist_view:   '我的关注',
  watchlist_add:    '加入关注',
  watchlist_remove: '移出关注',
  alert_set:        '创建提醒',
  alert_list:       '我的提醒',
  portfolio_view:   'Alpaca 持仓',
  pnl_view:         '当日盈亏',
  settings:         '系统设置',
}

export function intentLabel(intent: string | null | undefined): string | null {
  if (!intent || intent === 'unknown') return null
  return INTENT_LABELS[intent] ?? null
}
