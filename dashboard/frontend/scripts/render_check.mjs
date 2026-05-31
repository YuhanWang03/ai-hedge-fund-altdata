// Post-build sanity check for the trace event renderer. Renders each
// non-trivial event type with a representative payload and asserts the
// expected substrings appear in the HTML. Run with:
//
//   cd dashboard/frontend && npx tsx scripts/render_check.mjs
//
// Use when a redeploy doesn't seem to reflect the latest frontend changes,
// or when you want to confirm a particular event_type renders as expected.

import { renderToString } from 'react-dom/server'
import React from 'react'
import { TraceEvent } from '../src/components/TracePanel/TraceEvent.tsx'
import { PipelineBar } from '../src/components/PipelineBar.tsx'
import { eventToStep, isSessionComplete, shouldHighlight } from '../src/event_to_step.ts'

const cases = [
  {
    name: 'transform · cusip_aggregate',
    event: {
      type: 'transform', session_id: 's', seq: 1, ts_ms: 0,
      op: 'cusip_aggregate', before: 30, after: 29,
      manager: 'Berkshire Hathaway', accession: '25-26-226661',
    },
    expect: ['⟳ transform', 'cusip_aggregate', 'before', '30', 'after', '29', 'Berkshire Hathaway'],
  },
  {
    name: 'transform · detect_changes (with changes table)',
    event: {
      type: 'transform', session_id: 's', seq: 2, ts_ms: 0,
      op: 'detect_changes', current_positions: 29, prior_positions: 42,
      significant_changes: 7, manager: 'Berkshire Hathaway', quarter: '2026-Q1',
      changes: [
        { ticker: 'V',     issuer: 'VISA INC',                action: 'exit',     current_value: 0,            change_value: -2_900_000_000 },
        { ticker: 'MA',    issuer: 'MASTERCARD INC',          action: 'exit',     current_value: 0,            change_value: -2_300_000_000 },
        { ticker: 'UNH',   issuer: 'UNITEDHEALTH GROUP',      action: 'exit',     current_value: 0,            change_value: -1_700_000_000 },
        { ticker: 'DAL',   issuer: 'DELTA AIR LINES',         action: 'new',      current_value: 2_600_000_000, change_value: 2_600_000_000 },
        { ticker: 'GOOGL', issuer: 'ALPHABET INC',            action: 'increase', current_value: 15_600_000_000, change_value: 5_000_000_000 },
        { ticker: 'STZ',   issuer: 'CONSTELLATION BRANDS',    action: 'decrease', current_value: 94_900_000,   change_value: -50_000_000 },
      ],
    },
    expect: [
      'detect_changes', 'current_positions', '29', 'significant_changes', '7',
      'V', 'VISA INC', '清仓',
      'DAL', 'DELTA AIR LINES', '新进',
      'GOOGL', 'ALPHABET INC', '加仓',
      'STZ', 'CONSTELLATION BRANDS', '减仓',
      '$2.90B',  // exit V change_value formatted
      '$15.60B', // GOOGL current_value formatted
    ],
  },
  {
    name: 'render · portfolio_snapshot',
    event: {
      type: 'render', session_id: 's', seq: 3, ts_ms: 0,
      card: 'portfolio_snapshot', manager: 'Berkshire Hathaway', quarter: '2026-Q1',
      total_value_usd: 263_100_000_000, positions_shown: 10, positions_total: 29,
    },
    expect: ['▦ render', 'portfolio_snapshot', '$263.10B', 'positions_shown', '10', 'positions_total', '29'],
  },
  {
    name: 'render · institutional_summary',
    event: {
      type: 'render', session_id: 's', seq: 4, ts_ms: 0,
      card: 'institutional_summary', num_managers: 1, num_changes: 7,
    },
    expect: ['institutional_summary', 'num_managers', '1', 'num_changes', '7'],
  },
  {
    name: 'module_enter renders inside a bordered indigo box',
    event: {
      type: 'module_enter', session_id: 's', seq: 3, ts_ms: 0,
      name: '_r_thirteen_f', intent: 'thirteen_f',
    },
    expect: [
      '_r_thirteen_f',
      // Must have the same box treatment as api / llm / db_write cards.
      'rounded border', 'bg-indigo-50', 'border-indigo-200',
    ],
  },
  {
    name: 'event with explanation renders 📖 解析 disclosure',
    event: {
      type: 'transform', session_id: 's', seq: 5, ts_ms: 0,
      op: 'cusip_aggregate', before: 30, after: 29,
      explanation: {
        source: '上一步 EDGAR Filing.obj 返回的原始持仓列表',
        how:    'Python 字典按 CUSIP 分组',
        what:   '去重后的持仓列表',
        store:  'scheduler 路径写 edgar.db',
        next:   '送入 detect_changes() 跟前一季度对比',
      },
    },
    // <details> + summary label + each of the 5 emoji rows + the actual text
    expect: [
      '<details', '📖 解析',
      '📍', '上一步 EDGAR Filing.obj',
      '🔧', 'CUSIP 分组',
      '📦', '去重后的持仓列表',
      '💾', 'edgar.db',
      '➡️', 'detect_changes',
    ],
  },
  {
    name: 'event without explanation must NOT render disclosure',
    event: {
      type: 'transform', session_id: 's', seq: 6, ts_ms: 0,
      op: 'unknown_op', extra: 'something',
      // no explanation field
    },
    expect: ['unknown_op'],
    expectAbsent: ['📖 解析', '<details'],
  },
  {
    name: 'transform with explanation must NOT dump it as a chip',
    event: {
      type: 'transform', session_id: 's', seq: 7, ts_ms: 1234567,
      op: 'cusip_aggregate', before: 30, after: 29,
      manager: 'Berkshire Hathaway', accession: '25-26-226661',
      explanation: {
        source: 'EDGAR Filing.obj',
        how:    'Python 字典按 CUSIP 分组',
        what:   '去重后的持仓列表',
        store:  'edgar.db',
        next:   'detect_changes()',
      },
    },
    expect: [
      // Heading + the four legitimate chip fields.
      'cusip_aggregate', 'before', '30', 'after', '29', 'Berkshire Hathaway', '25-26-226661',
      // Formatted disclosure shows up exactly once with the prose.
      '📖 解析', '📍', '🔧', '📦', '💾', '➡️',
      'EDGAR Filing.obj', 'CUSIP 分组', '去重后的持仓列表', 'edgar.db', 'detect_changes()',
    ],
    expectAbsent: [
      // The raw JSON-dump form: "explanation: {...}" must NOT appear in
      // the chip strip. (The formatted block uses 来源/方式/etc. labels,
      // never the literal English key "explanation".)
      'explanation:',
      '{"source"',
      // Wire metadata must never leak into chips either.
      'session_id:', 'seq:', 'ts_ms:',
    ],
  },
  {
    name: 'render card with explanation must NOT dump it as a chip',
    event: {
      type: 'render', session_id: 's', seq: 8, ts_ms: 0,
      card: 'portfolio_snapshot', manager: 'Berkshire', quarter: '2026-Q1',
      positions_shown: 10, positions_total: 29, total_value_usd: 263_100_000_000,
      explanation: {
        source: 'EDGAR 取回并 CUSIP 聚合后的持仓列表',
        how:    'Python 按市值排序取前 N',
        what:   '组合卡：Top N 持仓 + 总组合价值',
        store:  '不持久化',
        next:   '塞进 chat_message 推送',
      },
    },
    expect: ['portfolio_snapshot', '$263.10B', '📖 解析', 'CUSIP 聚合'],
    expectAbsent: ['explanation:', '{"source"'],
  },
]

const pipelineCases = [
  {
    name: 'pipeline_bar_unknown_intent_hidden',
    props: { intent: 'unknown', events: [] },
    expect: [],          // component returns null
    expectExactEmpty: true,
  },
  {
    name: 'pipeline_bar_missing_intent_hidden',
    props: { intent: '', events: [] },
    expectExactEmpty: true,
  },
  {
    name: 'pipeline_bar_thirteen_f_progressive',
    props: {
      intent: 'thirteen_f',
      events: [
        { type: 'session_start',     session_id: 's', seq: 1, ts_ms: 0 },
        { type: 'llm_call',          session_id: 's', seq: 2, ts_ms: 0, role: 'intent_classifier', prompt_preview: '' },
        { type: 'intent_classified', session_id: 's', seq: 3, ts_ms: 0, intent: 'thirteen_f' },
        { type: 'api_call',          session_id: 's', seq: 4, ts_ms: 0, provider: 'edgar', endpoint: 'Company.get_filings' },
      ],
    },
    // All 8 labels appear; emerald-500 for done, blue-500+animate-pulse for active.
    expect: [
      '输入', '意图', 'EDGAR', '聚合', '对比', '解读', '卡片', '回复',
      'bg-emerald-500',   // at least one done pill
      'bg-blue-500',      // the next-to-fire (aggregate) is active
      'animate-pulse',    // active pulses
    ],
    expectAbsent: [
      'Replay',           // not a cached run
    ],
  },
  {
    // Order verified against v2/monitoring/attributor.py:attribute():
    //   _search_news → _entity_filter → _synthesize (Generator) →
    //   _verify_reasons (Verifier) → memory.remember.
    // ChromaDB write surfaces as db_write { db: 'chroma', fn: 'remember' }.
    name: 'pipeline_bar_explain_move_full_done',
    props: {
      intent: 'explain_move',
      events: [
        { type: 'session_start',     session_id: 's', seq: 1,  ts_ms: 0 },
        { type: 'intent_classified', session_id: 's', seq: 2,  ts_ms: 0, intent: 'explain_move' },
        { type: 'api_call',          session_id: 's', seq: 3,  ts_ms: 0, provider: 'fd',     endpoint: 'CachedFDClient.get_prices' },
        { type: 'api_call',          session_id: 's', seq: 4,  ts_ms: 0, provider: 'tavily', endpoint: 'search' },
        // get_company_facts isn't in the pipeline (it's a side-fetch for
        // entity matching) — must NOT light any pill.
        { type: 'api_call',          session_id: 's', seq: 5,  ts_ms: 0, provider: 'fd',     endpoint: 'CachedFDClient.get_company_facts' },
        { type: 'llm_call',          session_id: 's', seq: 6,  ts_ms: 0, role: 'generator' },
        { type: 'llm_call',          session_id: 's', seq: 7,  ts_ms: 0, role: 'verifier' },
        { type: 'db_write',          session_id: 's', seq: 8,  ts_ms: 0, db: 'chroma', fn: 'remember' },
        { type: 'render',            session_id: 's', seq: 9,  ts_ms: 0, card: 'anomaly_card' },
        { type: 'chat_message',      session_id: 's', seq: 10, ts_ms: 0, text: 'done' },
        { type: 'session_end',       session_id: 's', seq: 11, ts_ms: 0 },
      ],
    },
    expect: ['输入', '意图', '行情', '新闻', '归因', '评级', '记忆', '卡片', '回复', 'bg-emerald-500'],
    // After session_end: no active marker, no pending pills should remain
    // (every step in the pipeline was triggered).
    expectAbsent: ['animate-pulse', 'bg-blue-500', 'text-slate-400'],
  },
  {
    name: 'pipeline_bar_cached_replay',
    props: { intent: 'etf_view', events: [], cached: true },
    expect: ['输入', '意图', 'ARK', '对比', '卡片', '回复', 'bg-emerald-500', 'Replay'],
    expectAbsent: ['animate-pulse', 'bg-blue-500'],
  },
  {
    name: 'pipeline_bar_pill_self_highlight',
    // Cached so every pill is done; ARK pill is the highlighted one.
    props: {
      intent: 'etf_view', events: [], cached: true,
      highlightedStepId: 'ark',
    },
    expect: [
      'pill-glass-highlight',   // the highlighted pill picks up the glass class
    ],
  },
  {
    name: 'pipeline_bar_no_pill_highlight_when_none_active',
    props: { intent: 'etf_view', events: [], cached: true },
    expectAbsent: ['pill-glass-highlight'],
  },
]

let failures = 0
for (const c of pipelineCases) {
  const html = renderToString(React.createElement(PipelineBar, c.props))
  if (c.expectExactEmpty) {
    if (html === '' || html === '<!-- -->' || html === null) {
      console.log(`✓ ${c.name}`)
    } else {
      failures++
      console.log(`✗ ${c.name}`)
      console.log(`    expected empty render, got: ${html}`)
    }
    continue
  }
  const missing = (c.expect ?? []).filter((n) => !html.includes(n))
  const unwanted = (c.expectAbsent ?? []).filter((n) => html.includes(n))
  if (missing.length === 0 && unwanted.length === 0) {
    console.log(`✓ ${c.name}`)
  } else {
    failures++
    console.log(`✗ ${c.name}`)
    if (missing.length) console.log(`    missing: ${missing.map((m) => JSON.stringify(m)).join(', ')}`)
    if (unwanted.length) console.log(`    must-not-contain but found: ${unwanted.map((m) => JSON.stringify(m)).join(', ')}`)
    console.log(`    html: ${html.slice(0, 800)}…`)
  }
}

for (const c of cases) {
  const html = renderToString(React.createElement(TraceEvent, { event: c.event }))
  const missing = c.expect.filter((needle) => !html.includes(needle))
  const unwanted = (c.expectAbsent ?? []).filter((needle) => html.includes(needle))
  if (missing.length === 0 && unwanted.length === 0) {
    console.log(`✓ ${c.name}`)
  } else {
    failures++
    console.log(`✗ ${c.name}`)
    if (missing.length) console.log(`    missing: ${missing.map((m) => JSON.stringify(m)).join(', ')}`)
    if (unwanted.length) console.log(`    must-not-contain but found: ${unwanted.map((m) => JSON.stringify(m)).join(', ')}`)
    console.log(`    html: ${html}`)
  }
}

// ---- eventToStep pure-logic mappings --------------------------------------

const mapCases = [
  // New mappings introduced this round.
  { ev: { type: 'module_enter', name: '_r_explain_move' }, want: 'classify' },
  { ev: { type: 'module_enter', name: '_r_thirteen_f' },   want: 'classify' },
  { ev: { type: 'module_exit',  name: '_r_explain_move' }, want: 'reply' },
  { ev: { type: 'module_exit',  name: '_r_thirteen_f' },   want: 'reply' },
  // Pre-existing mappings users asked for explicit coverage on.
  { ev: { type: 'db_read',  table: 'watchlist' },          want: 'sqlite_read' },
  { ev: { type: 'db_query', table: 'alerts' },             want: 'sqlite_read' },
  { ev: { type: 'validate', what: 'ticker_format' },       want: 'validate' },
  // Sanity: ChromaDB write resolves via db label (regression guard for
  // the "remember" vs "anomaly_memory_remember" bug fixed last round).
  { ev: { type: 'db_write', db: 'chroma', fn: 'remember' }, want: 'memory' },
  // Sanity: generic SQLite write doesn't collide with memory.
  { ev: { type: 'db_write', db: 'edgar.db', fn: 'save_filing' }, want: 'sqlite_write' },
  // Sanity: events without any pipeline meaning return null.
  { ev: { type: 'session_end' },                            want: null },
  { ev: { type: 'error', where: 'tavily' },                 want: null },
]

for (const { ev, want } of mapCases) {
  const got = eventToStep(ev)
  if (got === want) {
    console.log(`✓ eventToStep: ${JSON.stringify(ev)} → ${want}`)
  } else {
    failures++
    console.log(`✗ eventToStep: ${JSON.stringify(ev)} → ${got} (expected ${want})`)
  }
}


// ---- Liquid-glass highlight: logic + rendered class -----------------------

const highlightCases = [
  {
    name: 'glass_highlight_before_completion',
    // Mid-flight events: edgar fired but session_end hasn't arrived.
    events: [
      { type: 'session_start',     session_id: 's', seq: 1, ts_ms: 0 },
      { type: 'intent_classified', session_id: 's', seq: 2, ts_ms: 0, intent: 'thirteen_f' },
      { type: 'api_call',          session_id: 's', seq: 3, ts_ms: 0, provider: 'edgar', endpoint: 'Company.get_filings' },
    ],
    cached: false,
    highlightedStepId: 'edgar',
    expectComplete: false,
    expectHighlight: false,
  },
  {
    name: 'glass_highlight_after_completion',
    events: [
      { type: 'session_start',     session_id: 's', seq: 1, ts_ms: 0 },
      { type: 'intent_classified', session_id: 's', seq: 2, ts_ms: 0, intent: 'thirteen_f' },
      { type: 'api_call',          session_id: 's', seq: 3, ts_ms: 0, provider: 'edgar', endpoint: 'Company.get_filings' },
      { type: 'api_call',          session_id: 's', seq: 4, ts_ms: 0, provider: 'edgar', endpoint: 'Filing.obj' },
      { type: 'session_end',       session_id: 's', seq: 5, ts_ms: 0 },
    ],
    cached: false,
    highlightedStepId: 'edgar',
    expectComplete: true,
    expectHighlight: true,
    // Both api_call(edgar) events must light up; the session_start must not.
    highlightSeqs: [3, 4],
    nonHighlightSeqs: [1, 2, 5],
  },
  {
    name: 'glass_highlight_cached_replay',
    events: [],
    cached: true,
    highlightedStepId: 'ark',
    expectComplete: true,
    // No events to assert per-row, but the predicate should be ready to fire.
    expectHighlight: true,
    sampleEvent: { type: 'api_call', session_id: 's', seq: 1, ts_ms: 0, provider: 'ark_csv', endpoint: 'fetch' },
  },
]

for (const c of highlightCases) {
  const complete = isSessionComplete(c.events, !!c.cached)
  if (complete !== c.expectComplete) {
    failures++
    console.log(`✗ ${c.name}`)
    console.log(`    isSessionComplete expected ${c.expectComplete}, got ${complete}`)
    continue
  }

  let ok = true

  // Per-event predicate.
  if (c.highlightSeqs) {
    for (const ev of c.events) {
      const want = c.highlightSeqs.includes(ev.seq)
      const got = shouldHighlight(ev, c.highlightedStepId, complete)
      if (want !== got) {
        ok = false
        console.log(`    ${c.name}: shouldHighlight(seq=${ev.seq}) want=${want} got=${got}`)
      }
    }
  }

  // Cached path needs a sample event to confirm the predicate works.
  if (c.sampleEvent) {
    const got = shouldHighlight(c.sampleEvent, c.highlightedStepId, complete)
    if (got !== c.expectHighlight) {
      ok = false
      console.log(`    ${c.name}: sample shouldHighlight expected ${c.expectHighlight}, got ${got}`)
    }
  }

  // For the before-completion case, every event must NOT highlight.
  if (c.expectHighlight === false && c.events.length > 0) {
    for (const ev of c.events) {
      if (shouldHighlight(ev, c.highlightedStepId, complete)) {
        ok = false
        console.log(`    ${c.name}: seq=${ev.seq} unexpectedly highlighted before completion`)
      }
    }
  }

  // Finally, render a representative event and check the DOM class.
  const sample = c.sampleEvent ?? c.events.find((e) => e.type === 'api_call')
  if (sample) {
    const isHighlighted = shouldHighlight(sample, c.highlightedStepId, complete)
    const html = renderToString(React.createElement(TraceEvent, {
      event: sample,
      highlighted: isHighlighted,
    }))
    const hasGlassClass = html.includes('glass-highlight')
    if (hasGlassClass !== isHighlighted) {
      ok = false
      console.log(`    ${c.name}: rendered glass-highlight class mismatch — class=${hasGlassClass}, expected=${isHighlighted}`)
    }
  }

  if (ok) console.log(`✓ ${c.name}`)
  else { failures++; console.log(`✗ ${c.name}`) }
}


const totalChecks = cases.length + pipelineCases.length + highlightCases.length + mapCases.length
if (failures > 0) {
  console.error(`\n${failures} render check(s) failed.`)
  process.exit(1)
}
console.log(`\nAll ${totalChecks} render checks passed.`)
