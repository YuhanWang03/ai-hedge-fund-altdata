// Static pipeline definitions per intent. Each intent maps to an ordered
// list of step IDs; STEP_DEFS holds the visual metadata for each step.
// Step IDs are the contract between pipelines (this file) and the
// event-to-step mapper (event_to_step.ts).

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
  explain_move:     ['input', 'classify', 'price', 'insider', 'news', 'verify', 'generate', 'memory', 'render', 'reply'],
  summary:          ['input', 'classify', 'price', 'fundamentals', 'insider', 'news', 'llm_interpret', 'render', 'reply'],
  chain:            ['input', 'classify', 'price', 'llm_propose', 'news', 'filter', 'render', 'reply'],
  etf_view:         ['input', 'classify', 'ark', 'detect', 'render', 'reply'],
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
