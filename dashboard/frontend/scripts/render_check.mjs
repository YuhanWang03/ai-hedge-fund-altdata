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

let failures = 0
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

if (failures > 0) {
  console.error(`\n${failures} render check(s) failed.`)
  process.exit(1)
}
console.log(`\nAll ${cases.length} render checks passed.`)
