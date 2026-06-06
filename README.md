# AI Hedge Fund · Alternative Data Agent System

A production-grade alternative-data intelligence platform delivered through a Telegram bot. **10 shipped phases** combine fundamental screening, anomaly detection, portfolio risk, SEC monitoring, macro data, and ARK rebalance signals into **21 process-isolated cron jobs**, with five layers of hallucination defense keeping every claim grounded in primary sources.

Built as a portfolio project to demonstrate end-to-end ownership of a multi-source data pipeline, dual-LLM verification architecture, and 24/7 deployed system.

[中文版 README](./README_zh.md)

**Hero numbers**: 10 phases shipped · 21 cron jobs · 23 NL intents · 5-layer defense · 474 sandbox tests · **~$22/month** total ops cost

---

## What it does

Three concurrent services on a single $6/month VPS — **Scheduler** (21 cron jobs spanning 08:00–21:00 ET) + **Streamer** (minute-level intraday alert + anomaly scan) + **Telegram Bot** (25 slash + 23-intent NL). All three share 7 SQLite databases (WAL) + ChromaDB.

### Scheduled pushes (Cron Jobs full table)

Sorted by US/Eastern time (agent details in [## Feature Reference](#feature-reference) → Cron Agents). ⚠️ marks current implementation that's simplified or requires data accumulation — see footnote below:

| Time · Frequency | ID | Name | Function | Family · Pushes |
|---|---|---|---|---|
| **02:00 UTC** · daily | ⑥ | Archive Cleanup | Purge > 90-day archive rows | infra · 🔇 no push |
| **08:00 ET** · mon-fri | ⑦ | Earnings Reminders | watchlist + holdings D-3/D-1/D-0 alerts | Earnings · ✅ |
| **08:30 ET** · mon-fri | ⑬ | ARK Alerts | ARK 4 funds material rebalance (new/exit/±20%) | ARK · ✅ pre-market |
| **09:00 ET** · mon-fri | ⑮ | Macro Release Scanner | CPI/PCE/NFP/GDP/PPI/FOMC release interpretation (σ ladder) | Macro · ✅ on hit day |
| **09:30 ET** · **thu** | ⑯ | Macro Initial Claims | ICSA weekly initial unemployment + 4W MA | Macro · ✅ |
| **16:25 ET** · mon-fri | ⑨b | Positions Snapshot | Daily holdings snapshot (input to ⑩ attribution) | Portfolio · 🔇 archive only |
| **16:30 ET** · mon-fri | ⑭ | Macro Daily Snapshot | VIX/yields/commodities EOD + 4 anomaly flags | Macro · ✅ |
| **16:45 ET** · mon-fri | 📋 | P2 Digest | Roll up day's P2 pushes into one card | infra · ✅ rollup |
| **17:00 ET** · mon-fri | ⑤ | ETF Daily Snapshot | 4 ARK fund holdings CSV persist (⑬ baseline) | Early signals · 🔇 dashboard only |
| **17:05 ET** · mon-fri | ⑪ | SEC 8-K Scanner | Today's 8-K + 24-item priority + 5.02 LLM NER | SEC · ✅ |
| **17:30 ET** · mon-fri | ① | Daily Screen | TECH_30 hard-rule screen + sector benchmark | Early signals · ✅ |
| **17:35 ET** · mon-fri | ② | Anomaly Monitor | Anomaly detect + Tavily multi-source attribution + Verifier | Early signals · ✅ |
| **17:45 ET** · mon-fri | ⑫ | SEC Form 4 Scanner | Insider P/S individual + same-day ≥3 distinct cluster | SEC · ✅ |
| **18:00 ET** · **mon** | ③ | Lateral Expansion | LLM supply-chain lateral + Tavily co-occurrence verify | Early signals · ✅ |
| **18:00 ET** · **tue/fri** | ④ | Institutional 13F | 10 manager quarterly holdings + diff (CUSIP-aggregated) | Early signals · ✅ |
| **18:30 ET** · **sun** | ④b | 13F Backfill | 13F weekly refill + catch amended filings | Early signals · 🔇 maintenance |
| **18:30 ET** · mon-fri | ⑨ | Portfolio Risk | Portfolio risk (concentration/drawdown/earnings 7d/sector) | Portfolio · ✅ |
| **19:00 ET** · **fri** | ⑩ | Portfolio Weekly | Weekly review + per-position attribution ⚠️ | Portfolio · ✅ |
| **19:15 ET** · **fri** | ⑫b | SEC Insider Weekly Digest ⚠️ | Aggregate this week's ⑫ pushes (title-only simplification) | SEC · ✅ |
| **19:30 ET** · **fri** | ⑰ | Macro Weekly Recap | This week released + next week preview + intraweek yields delta | Macro · ✅ |
| **21:00 ET** · mon-fri | ⑧ | Earnings Summaries | LLM summary + 10-Q MD&A diff + going_concern → P0 | Earnings · ✅ |

**⚠️ Partial implementation / data accumulation required**:

- **⑩ Portfolio Weekly** — per-position attribution uses 3-state gating: ≥5 days of ⑨b snapshots → full best/worst/net; 1–4 days → "归因数据累积中 (N/5 天)" placeholder; 0 days → fully silent. **First complete week after deployment is required for full attribution data.**
- **⑫b SEC Insider Weekly Digest** — currently **title-only simplification** (⑫ archive `trace_json` doesn't persist per-transaction codes, only push titles are queryable). Full per-A/M/F/G/C breakdown + insider-name dimension aggregation is deferred to **Phase 3.5.5** (see [Roadmap](#roadmap)).

**⏳ Planned but not yet implemented cron jobs** (see [## Roadmap](#roadmap)):

| Time · Frequency | ID | Name | Function | Trigger |
|---|---|---|---|---|
| **FOMC day +6h** · ad hoc | ⑮b | FOMC Transcript Follow-up | Powell presser transcript scrape supplemental card | Phase 4.5 — after 06-17 FOMC battle test |
| **16:35 ET** · mon-fri | ⑭b | Market Regime Detection | VIX+yields+breadth composite regime judgment + switch alerts | Phase 5b — after Phase 4 runs ≥ 4 weeks (avoid overfit) |

**Time design**: 17:00–19:30 ET is the dense main push window (post-close data lands fastest); 08:00–09:30 ET is pre-market (earnings reminders + ARK rebalance + macro releases); 16:25–16:45 ET is silent post-close batch; ⑧ runs at 21:00 ET because FD earnings data typically fully lands between 19:30–21:00 ET.

### Intraday Streamer

Runs in parallel to the scheduler. Polls every 60s during 9:30–16:00 ET Mon-Fri; 5-min idle outside. **No LLM/Tavily during market hours** — deep attribution at 17:35 ET. Details: [## Feature Reference → Intraday Streamer](#intraday-streamer-details).

| Trigger | Source | Freq | When fires |
|---|---|---|---|
| User-set price alerts | Alpaca `get_stock_latest_trade` | 1/min | Created via `/alert NVDA 130 above` or NL |
| TECH_30 auto anomaly | Alpaca latest-trade × 29 tickers | 1/min | Dual threshold: ≥3% move AND ≥2.5× volume pace |

### Telegram interactive query

24/7 long-polling bot, single-user (chat-ID filter), 25 slash + 23 NL intents + 10 manager aliases. Details: [## Feature Reference → Telegram interface](#telegram-interface-details).

| Category | Slash | NL example |
|---|---|---|
| Watchlist | `/watchlist`, `/add NVDA`, `/remove TSLA` | `我关注了哪些股票` |
| Analysis | `/why`, `/summary`, `/chain`, `/13f`, `/holders`, `/etf` | `NVDA 为什么跌？`, `Cathie 今天买啥` |
| Alerts | `/alert`, `/alerts`, `/alert_remove` | `提醒我 NVDA 突破 130` |
| Account | `/portfolio`, `/pnl [day\|week\|month]`, `/risk` | `我的当日盈亏`, `组合风险怎么样` |
| Earnings | `/earnings AAPL`, `/earnings` (calendar) | `苹果什么时候发财报` |
| SEC | `/8k TICKER`, `/insiders TICKER [days]` | `NVDA 内部人交易` |
| Macro | `/macro`, `/cpi`, `/fomc`, `/yields` | `宏观怎么样`, `最近 CPI` |

NL classifier: DeepSeek temperature=0 + JSON + **whitelist enum validation** — outside-whitelist → `unknown` (bounded behavior).

---

## System architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-scheduler.service                 │
│                       (21 cron jobs · see table below)           │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-streamer.service                  │
│  9:30 - 16:00 ET Mon-Fri · every 60s                             │
│   ├─ User /alert triggers     · Alpaca latest-trade × ticker     │
│   └─ TECH_30 auto-scan        · dual-threshold + sector contra   │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Shared SQLite + ChromaDB                       │
└──────────────────────────────────────────────────────────────────┘
                              ▲
                              │
┌──────────────────────────────────────────────────────────────────┐
│                      hedge-fund-bot.service                      │
│  25 slash commands + 23 NL intents · DeepSeek T=0 · strict-enum  │
└──────────────────────────────────────────────────────────────────┘

External: financialdatasets.ai · yfinance · SEC EDGAR · ARK CSV CDN
          Alpaca · FRED · Tavily News API · DeepSeek LLM · OpenAI embeddings
```

---

## Hallucination defense + Priority

### 5-layer signal defense (system-wide)

LLMs **never produce numeric facts directly** — they only classify, write qualitative narration, and select between candidate sources. **All numbers come from primary data**.

1. **Entity filter** — news must contain target ticker AND ≥1 company-name token before reaching the Verifier.
2. **Source-tier scoring** — Verifier scores Tier 1 (SEC/Reuters/Bloomberg/WSJ/FT/CNBC) / Tier 2 (MarketWatch/SeekingAlpha/Yahoo/Fool/Forbes) / Tier 3 (others, discarded).
3. **Generator-Verifier split** — separate Verifier validates Generator output against the source list; strict prompt forbids numbers not in input.
4. **Template Fill** — narrator outputs only qualitative phrases; Python f-string injects all numbers.
5. **Stale-data chip** — filings > 180 days → ⚠️; > 365 days → stale-N-months. Prevents 2023 13F being read as current state.

### LLM 4-layer defense (Macro Agent only)

`v2/macro/summarizer.py` adds macro-specific layers because release interpretation is especially sensitive to numeric leakage: **L1 Template-Fill** (`_SYSTEM_PROMPT` forbids numbers + forward predictions; 4 fixed JSON fields ≤40 chars each); **L2 Post-Parse Reject** (regex scans for "will/expect/may.*[rise/fall]" + digit leakage → fallback to neutral); **L3 FOMC bypass** (hawkish/dovish verdict fully LLM-free — Python diff over 15 KEY_PHRASES + SEP dot-plot extract + Tavily majority vote across 8 trusted domains); **L4 Historical analog** (deferred to Phase 4.5). LLM failure → `_NEUTRAL_FALLBACK`; priority isn't missed because sigma is Python-computed.

### Push priority P0/P1/P2/P3

Every push computes `importance_score` (0–100) → 4 tiers. **P0** (≥80) immediate push + 🚨🚨🚨 + dashboard red frame (price alert, big loss, FOMC, ARK liquidation of held, material 8-K). **P1** (60–79) immediate push (anomaly attribution, watchlist earnings, 13F changes). **P2** (40–59) archive + 16:45 ET digest rollup. **P3** (<40) archive only, dashboard hidden.

Base score + metadata adjustments: held +15, watchlist +10, surprise ≥10% +15, **daily loss ≥5% +30 (→P0)**, multi-factor stack up to +75, **going_concern +20**, material_weakness +15, **ARK multi-fund +15**, ARK large_new_position +10. Pure Python rules; `notifier.send_text` without `priority=` defaults to P1 (backward-compat).

---

## Feature Reference

Deep-dive on the three functional modules: **Cron Agents** (21 jobs organized by family — each section has data sources, trigger logic, priority ladder, output examples, bot interface) + **Intraday Streamer** (A user alerts + B auto-scan) + **Telegram interface** (full slash list + 23 NL examples + manager aliases).

### Five early post-market agents

- **① Daily Screen (Mon-Fri 17:30 ET)** — TECH_30 hard-rule screen + 9 qualitative tags + LLM Template-Fill narration (numbers Python-injected) + sector-relative chips + Tavily news.
- **② Anomaly Monitor (Mon-Fri 17:35 ET)** — volume spikes (≥3× 30d) / 52-week extremes / insider clusters. Per anomaly: sector chip + multi-source attribution (Tavily → Verifier tier scoring → Generator with Tier 1+2 only) → persists to ChromaDB with deterministic ID `{ticker}_{date}`.
- **③ Lateral Expansion (Mon 18:00 ET)** — anomaly seeds → DeepSeek proposes supply-chain neighbors → Tavily co-occurrence verification → market cap / revenue / margin thresholds.
- **④ Institutional 13F (Tue/Fri 18:00 ET) + ④b Backfill (Sun 18:30 ET)** — 10 famous managers (Berkshire, Burry, Ackman, Einhorn, Renaissance, Two Sigma, D.E. Shaw, Citadel, Coatue, ARK). QoQ diffs → 新进/加仓/减仓/清仓 → DeepSeek 10-word interpretations. **Tracks $1.29T AUM × 17,329 positions**.
- **⑤ ETF Daily Snapshot (Mon-Fri 17:00 ET)** — silently fetches 4 ARK funds' (ARKK/ARKW/ARKG/ARKF) daily CSVs to `etf.db.snapshots`. Per-(fund, date, ticker) time series enabling **24-hour rebalance detection** (vs 45-day 13F lag). Underlying data for ⑬.

---

### Earnings Agent (⑦⑧)

| Cron | Time | Behavior | Priority |
|---|---|---|---|
| ⑦ Reminders | Mon-Fri **08:00 ET** | D-3/D-1/D-0 reminders across watchlist + holdings | D-3 = P2, D-1/D-0 = P1 |
| ⑧ Summaries | Mon-Fri **21:00 ET** | Post-release LLM summary + 200-day-lookback 10-Q MD&A diff | Base P1 (70); \|surprise\| ≥10% → P0 (+30); going_concern +20 / material_weakness +15 |

**Data**: yfinance `Ticker.calendar` (forward), FD `get_earnings` + `get_earnings_history`, Tavily transcript URL, SEC EDGAR via edgartools (10-Q MD&A + Risk Factors diff).

**10-Q integration**: every summary reverse-fetches the ticker's most recent 10-Q within 200 days and diffs MD&A (Part I Item 2) + Risk Factors (Part II Item 1A) vs the prior quarter. Card appends `📋 10-Q MD&A 关键变化` block: top-3 added paragraphs (each capped at 80 chars + "…"), new risk-factor heading count, conservative auditor flags (regex matches for "going concern" / "material weakness"). 10-Q fetch failure → block silently omitted; earnings card still ships. Auditor flags route directly into priority: `has_going_concern` +20 (P0), `has_material_weakness` +15.

**Output (BEAT held + 15% surprise + 10-Q, P0)**:

```
🚨🚨🚨 🟢 财报发布 · AAPL · BEAT
EPS：2.10 vs 预期 1.80 (+16.7%) · 营收：$95.00B vs 预期 $91.00B (+4.4%)
最近 4 季：BEAT → BEAT → MISS → BEAT
👍 连续 BEAT，Services 加速  👎 iPhone 出货指引偏保守
📋 10-Q MD&A 关键变化
  ➕ Revenue growth driven by Services and wearables segments outpacing…
  📌 1 个新 risk factor 段落
```

**Bot interface** (read-only): `/earnings AAPL`, `/earnings`. **Engineering**: yfinance / FD failure → silent skip per ticker; D-N recomputed daily; FD lag → P2 "pending" placeholder + auto-retry next 21:00 ET; Template-Fill mode (LLM only writes qualitative bull/bear/narrative); cross-quarter dedup via `PRIMARY KEY (ticker, report_period)`.

---

### Portfolio Risk Agent (⑨⑨b⑩)

| Cron | Time | Behavior | Priority |
|---|---|---|---|
| ⑨ Risk | Mon-Fri **18:30 ET** | Real-time RiskReport (drawdown with today realtime fix) | See ladder |
| ⑨b Snapshot | Mon-Fri **16:25 ET** | Silent backend: writes daily holdings to `positions_snapshot` table | No push (archive only) |
| ⑩ Weekly | Fri **19:00 ET** | Weekly recap + monthly + 1M drawdown + sector exposure + per-position attribution | Fixed P1 (floor) |

**Data**: Alpaca `account` + `positions` (TOTAL = invested + cash; `invested_value` is derived @property), `portfolio_history(1M, 1D)`, 90-ticker sector ETF mapping (with OTHER bucket), reuses `v2.earnings.calendar` for 7-day earnings risk.

**Priority ladder (⑨)**: normal day P2/55; top_1 ≥30% +20 → P1; **daily_pnl ≤ -5% +30 → P0**; 1M drawdown ≥10% +15 → P1; multi-factor stack up to +75 → P0 (capped at 100).

**Design**: single abnormal factor at most → P1 (reminder, not nag); single-day large loss OR multi-factor → P0.

**Outputs (⑨ multi-factor P0 / ⑩ weekly per-position attribution P1)**:

```
💼 组合风险 · 2026-06-04
今日 P/L 🔴 -1.78% · Top 1 NVDA 35.0% ⚠️ · SMH 38.0% ⚠️ · 1M 回撤 -12.00%
⚠️ 单票 NVDA > 30% / SMH 行业 > 30% / 1M 回撤 > 10%

📊 周 P&L 复盘 · 2026-06-06 — 本周 🟢 +1.50%
📊 本周 per-position 表现归因
  最佳: NVDA 🟢 +5.00% (贡献 🟢 +1.50%)
  最差: JPM  🔴 -2.00% (贡献 🔴 -0.30%)
  净贡献: 🟢 +1.40%
```

**Attribution 3-state gating**: ≥5 days ⑨b coverage → best/worst/net rows; 1-4 days → `归因数据累积中 (N/5 天)` placeholder; 0 days → fully silent (fresh-account contract).

**Drawdown realtime fix**: Phase 2's `compute_drawdown` only used EOD canonical series → today's intraday drop didn't enter → card could show "今日 P/L -3.72% / drawdown 0.00%" (real prod bug 2026-06-05). Fix: `compute_drawdown(broker, today_realtime_value=portfolio_value)` appends today's Alpaca value to the EOD series. Backward-compat preserved (`None` → Phase 2 behavior).

**Bot interface**: `/risk`, `/pnl [day|week|month]`. **Engineering**: `portfolio_value` is TOTAL (= invested + cash); weight denominator is invested_total (cash excluded from concentration); drawdown 5-layer sign defense; Alpaca-unavailable still pushes P2 "数据不全" card.

---

### SEC Monitoring (⑪⑫⑫b)

| Cron | Time | Behavior | Priority |
|---|---|---|---|
| ⑪ 8-K | Mon-Fri **17:05 ET** | Single ticker → one card aggregating all items | Base by max item tier, 5.02 senior_exec +15 |
| ⑫ Form 4 | Mon-Fri **17:45 ET** | P/S individual cards + same-day ≥3 cluster cards | Base 75 (P) / 50 (S) + magnitude / role / 10b5-1 |
| ⑫b Insider Digest | **Fri 19:15 ET** | Reverse-look this week's ⑫ pushes → weekly summary | Base 55 (P2); unusual ≥3 tickers → P1 |

**Data**: edgartools 5.31.5 (200ms throttle), reuses `EDGAR_IDENTITY` env var, separate `v2/sec/` module decoupled from `v2/institutional/`.

**24 8-K item codes priority table**: **P0** 1.03 Bankruptcy / 1.05 Cybersecurity / 2.04 Off-BS triggering / 3.01 Delisting / 4.02 Non-reliance / 5.01 Change in control / 5.02 Senior exec (LLM-confirmed). **P1** 1.01 Material agreement / 1.02 Termination / 2.01 Acquisition / 2.03 Material obligation / 2.05 Restructuring / 2.06 Impairment / 4.01 Auditor change / 5.02 Other officer. **P2** 3.02 Unregistered sales / 3.03 Material modification / 5.08 Director nominations / 7.01 Reg FD / 8.01 Other. **P3** 1.04 Mine safety / 5.03 Bylaw / 5.04 Blackout suspension / 5.05 Ethics / 5.07 Shareholder vote / 9.01 Financial exhibits.

**`2.02` strictly skipped** — handled by ⑧ at 21:00 ET. Mixed `{2.02, other}` filings keep card but annotate 2.02 as "(⑧ 处理)"; pure 2.02-only filings dropped.

**Multi-item single card**: HPE-style 8-K (1.01 + 2.02 + 5.02 + 7.01 + 9.01) merged into one card with `max_priority_tier` driving overall priority. **5.02 LLM extraction**: DeepSeek template-fill extracts `{departures, appointments, has_senior_exec}` (entities only). LLM failure → `extracted_meta={}` → bot card shows "(姓名待解析)"; priority does NOT auto-escalate (conservative).

**Form 4 noise vs signal** (Stage 0 calibrated 89 transactions): 83% is noise (A 44% / M 21% / F 15% / G 3% / C <1%) → `noise_summary` aggregate. Only P (8%) + S (8%) get individual cards. **Cluster detection**: same-ticker same-day same-direction with **≥3 distinct insiders** → one cluster card (coincidence on ≥3 insiders is extremely hard).

**⑫b weekly digest** (title-only fallback): Fri 19:15 ET reverse-aggregates this week's ⑫ pushes from `archive.pushes` by (ticker, direction). Phase 3.5 Stage 0 trace probe confirmed ⑫'s `trace_json` only stores aggregate "N signal · Y cluster · Z noise" — per-transaction A/M/F/G/C isn't persisted. To avoid expanding archive schema, ⑫b uses **pure title-only fallback** (regex parses `Form 4 · NVDA · 买入` titles); full per-code breakdown deferred to Phase 3.5.5. Empty weeks still push "本周平静" card.

**Priority ladders**: ⑪ 8-K base `sec_8k_p0=85` / p1=65 / p2=55 / p3=35; 5.02 senior_exec +5, amendment -5, held +15, watchlist +10. ⑫ Form 4 base `sec_form4_purchase=75` / `sale=50` / `cluster=75`; Purchase ≥$1M +25, CEO/CFO +10, 10b5-1 plan -10; Sale ≥$10M discretionary +15, 10b5-1 -5; Cluster ≥5 distinct +15. **Philosophy**: discretionary sale > 10b5-1 plan sale; cluster purchase > cluster sale (sale reasons are diverse).

**Bot interface**: `/8k AAPL`, `/insiders NVDA [days]` (bounded 7-365). **Engineering**: edgartools shape calibration (PascalCase `Code` column, Stage 0 fix); 5.02 LLM-fail → don't auto-escalate; 2.02 strict skip vs mixed retain; 10b5-1 footnote regex; 5+1 SEC formatters source-of-truth in `v2/sec/_bot_cards.py` (sandbox-runnable).

---

### Macro Agent (⑭⑮⑯⑰)

| Cron | Time | Behavior | Priority |
|---|---|---|---|
| ⑭ Daily Snapshot | Mon-Fri **16:30 ET** | VIX/yields/commodities + 4 anomaly flags | See ladder |
| ⑮ Release Scanner | Mon-Fri **09:00 ET** | release_calendar-gated CPI/PCE/NFP/GDP/PPI/FOMC | σ ladder |
| ⑯ Initial Claims | **Thu 09:30 ET** | Weekly ICSA + 4W MA smoothed | Base P2, σ>2 → P1 |
| ⑰ Macro Weekly | **Fri 19:30 ET** | This-week + next-week preview + intra-week VIX/yields delta | Fixed P1 (floor) |

**Data hybrid**: FRED canonical for Treasury yields + Fed Funds + CPI/PCE/NFP/GDP/PPI/Claims (avoids yfinance `^TNX` × 10 raw bug); yfinance for VIX realtime + DXY + WTI + Gold (15-min delay, FRED `VIXCLS` as EOD fallback); Tavily for FOMC sell-side aggregate across 8 trusted domains (Layer 3 defense); DeepSeek template-fill for release interpretation (Layer 1+2).

**release_calendar.py — hard-coded 2026 schedule**: cron paths must be deterministic + offline-runnable. `_seed_calendar.py` (one-shot) hits FRED `/release/dates` REST to generate 40 release entries covering 33 dates, pasted between `AUTOGEN BLOCK START/END` sentinels. `_LAST_UPDATED` + 6-month staleness check warns at cron startup. Claims (ICSA) skips the calendar (Thursday-deterministic trigger).

**Anomaly + σ ladders**: ⑭ `macro_snapshot_p3=35` / `macro_vix_spike=85` / `macro_curve_flip=65` — VIX ≥+30% +20 / +20% +10 → P0; T10Y2Y inversion +10 → P1; VIX +10% OR DGS10 ≥20bps → P1. ⑮ `macro_release_p2=55` / p1=65 / p0=85 — `|σ|≥3.0` +20 → P0; `≥2.0` +10 → P1; `≥1.0` +5; FOMC SEP shift +15 → P0; Tavily hawkish_unexpected +10 (FOMC path skips σ ladder, reads `sep_dot_plot_change`). ⑯ ICSA base P2; `|σ|≥2` → P1; holiday weeks silent skip. ⑰ fixed P1 floor (operator must see weekly recap, not event-driven).

**Bot interface**: `/macro` / `/cpi` / `/fomc` / `/yields`. **Engineering**: fredapi REST patch (httpx direct + 3-attempt backoff); 6 macro formatters source-of-truth in `v2/macro/_bot_cards.py` (sandbox-runnable); Layer 3 FOMC path is Python-only — fomc_parser.py diffs 15 KEY_PHRASES, extract_dot_plot_table regex-extracts dot plot, Tavily uses 8 trusted domains for keyword-count majority vote — **LLM never participates in the hawkish/dovish verdict**.

---

### ARK Rebalance Alerts (⑬)

| Cron | Time | Behavior | Priority |
|---|---|---|---|
| ⑬ ARK Alerts | Mon-Fri **08:30 ET** pre-market | Reads ⑤'s overnight `etf.db` baseline + today's CSV → diff → significant rebalances | Base 65 (P1); multi-fund / user-universe / large positions → P0 |

**Why**: ARK Invest is Cathie Wood's actively-managed ETF family, and unlike traditional ETFs **publishes full holdings every trading day**. Conviction trades surface **45 days ahead of 13F**. Sudden liquidation = strong negative; new position ≥0.5% = strong positive (especially small-cap); multi-fund same-direction = department-level conviction.

**Data — reuses ⑤ ETF infrastructure**: v2/etf/ has been fetching ARK CSVs + computing daily diffs + persisting to SQLite since Phase 0 (via ⑤ at 17:00 ET). Phase 5a reuses the entire infrastructure; only the alerts classifier + card formatter are new, **zero schema change, zero client/detector/tracker changes**. `SUPPORTED_FUNDS = ["ARKK", "ARKW", "ARKG", "ARKF"]` (ARKQ + ARKX deprecated by ARK on assets.ark-funds.com).

**4 action types + thresholds** (`v2/etf/alerts.py` single source of truth):

| Action | Threshold |
|---|---|
| `new_position` | today_weight ≥ 0.5% |
| `liquidated` | yesterday_weight ≥ 0.5% |
| `increase` / `decrease` | `|relative_change|` ≥ 20% |

**Priority escalation** (base `ark_alert_p1` = 65, P1):

| Trigger | Adjust | Reason |
|---|---|---|
| User universe (held ∪ watchlist) | +10 | `held_or_watchlist_ark` |
| Multi-fund coordination (≥2 funds same dir same ticker) | +15 | `multi_fund_coordination` |
| Large new position (today_weight ≥ 2%) | +10 | `large_new_position_X.X%` |
| Large liquidation (yesterday_weight ≥ 2%) | +10 | `large_liquidation_X.X%` |

**Design**: multi-fund coordination (different PMs, same direction, same day) is the strongest conviction signal → standalone +15. User-universe boost uses a distinct reason label (`held_or_watchlist_ark`, separate from generic `held_position`) so the audit trail distinguishes cron origins.

**Multi-fund detection**: `_mark_multi_fund` runs after per-fund classification and groups by `(ticker, direction)` (direction = buy/sell from action); ≥2 funds same direction same ticker → all matching alerts get `is_multi_fund=True`. **Opposite-direction same-ticker is explicitly NOT coordination** — different PMs reaching independent verdicts isn't a conviction signal; regression-pinned.

**Card UX**: 4 action templates (🟢 new / 🔴 liquidated / 📈 increase / 📉 decrease), 2 optional banners (multi-fund 🚨 above → user-universe 🟢 below). Summary card: total alerts + multi-fund count + held/watchlist count + buys-then-sells action distribution + user-universe ticker list + multi-fund coordination details + **(N/M ARK funds)** fraction for partial-failure transparency.

**Output (multi-fund + held P0, base 65 + held +10 + multi_fund +15 = 90)**:

```
📈 ARK 增持 · TSMC — Fund: ARKK · 今日权重: 3.50% (+30.0%)
🚨 多 Fund 协同（详见总览） · 🟢 持仓股 / 关注列表
增持: 100,000 shares · ≈ $15.0M · 昨日权重: 2.30%
```

**First-deploy edge** handled: `get_latest_snapshot_before` returns None → silent skip per fund (⑤ populates baseline at 17:00 ET, signals start next day).

---

### Intraday Streamer details

Runs in parallel to the scheduler. Polls every 60s during 9:30–16:00 ET Mon-Fri; 5-min idle outside.

**A. User-set price alerts** — `/alert NVDA 130 above` (or NL). Each minute: query unfired alerts, batch tickers into one Alpaca `get_stock_latest_trade`, run `alert_fire_check` which atomically marks any crossed alerts via `UPDATE … WHERE fired_at IS NULL`. One-shot SQL-layer semantics — never re-fires.

**B. TECH_30 automated anomaly scan** — every minute scans 29 tickers for dual-threshold anomalies (≥3% move AND ≥2.5× volume pace). Volume pace = `today_volume / (avg_30d × market_progress)` normalizes for partial session (no false positives in first hour). 30d baseline auto-refreshes every 7 trading days via Alpaca daily bars. Sector-relative chip auto-attached (`★ 逆势` chip when ticker reverse-moves vs sector ETF by ≥1.5pp). 30-min per-ticker cooldown via `intraday_cooldown` table prevents flood. **No LLM during market hours** — fast signals first, deep attribution at 17:35 ET (intraday news lags price).

Typical fire:

```
⚡ 盘中异动 · IBM · 10:15 ET
━━━━━━━━━━━━━━━━━━━━
📈 现价 $297.97  +7.45%  vs 开盘 $277.30
当日成交 28.4M  ·  节奏 3.6×  (已交易 11% 时段)
对比 XLK +0.40%  ·  差 ↑+7.05pp ★ 逆势

用 /why IBM 看盘后完整归因（17:35 ET 起效）
```

---

### Telegram interface details

NL layer classifies free-form text into **23 canonical intents** via DeepSeek temperature=0 + JSON output + whitelist enum validation. Outside-whitelist → `unknown` (bounded behavior).

#### Slash commands (full list)

- **Watchlist**: `/watchlist`, `/add NVDA`, `/remove TSLA`
- **Analysis**: `/why TICKER` (attribution) · `/summary TICKER` (multi-dim snapshot) · `/chain TICKER` (lateral) · `/13f MANAGER` · `/holders TICKER` · `/etf SYMBOL` · `/earnings AAPL` (single-ticker) · `/earnings` (14-day calendar)
- **Alerts**: `/alert NVDA 130 above` · `/alerts` · `/alert_remove ID`
- **Account**: `/portfolio` · `/pnl [day|week|month]` · `/risk`
- **SEC**: `/8k TICKER` (8-K + 5.02 NER) · `/insiders TICKER [days]` (bounded 7-365)
- **Macro**: `/macro` (dashboard) · `/cpi` · `/fomc` · `/yields`
- **Meta**: `/settings`, `/help`, `/start`

#### Natural-language examples (all 23 intents)

`NVDA 为什么跌？` → `explain_move` · NVDA; `看看 AAPL 怎么样` → `summary` · AAPL; `找一下 AMD 的产业链` → `chain` · AMD; `巴菲特最近买了什么` → `thirteen_f` · brk; `谁持有 NVDA` → `holders_view` · NVDA; `Cathie 今天买啥` → `etf_view` · ARKK; `提醒我 NVDA 突破 130` → `alert_set` · NVDA · 130 · above; `我设了哪些提醒` → `alert_list`; `看看 Alpaca 持仓` → `portfolio_view`; `我的当日盈亏` → `pnl_view`; `我关注了哪些股票` → `watchlist_view`; `最近有什么异动` → `find_anomalies`; `苹果什么时候发财报` → `earnings_view` · AAPL; `下周谁要发财报` → `earnings_calendar` · days_horizon=7; `组合风险怎么样` → `risk_view`; `这周亏了多少` → `pnl_period` · period=week; `本月赚了多少` → `pnl_period` · period=month; `AAPL 最近 8-K` → `eight_k_view` · AAPL; `NVDA 内部人交易` → `insider_view` · NVDA; `NVDA 过去 30 天 insider` → `insider_view` · NVDA · days_back=30; `宏观怎么样` → `macro_view`; `最近 CPI 数据` → `release_check` · release_type=cpi; `上次 FOMC 怎么说` → `release_check` · release_type=fomc; `NFP data this month` → `release_check` · release_type=nfp; `今天天气怎么样` → `unknown`.

#### Manager aliases (10 supported)

`brk/berkshire/buffett`, `burry/scion`, `ackman/pershing`, `einhorn/greenlight`, `renaissance/rentech`, `twosigma`, `deshaw/shaw`, `citadel`, `coatue`, `ark/cathie/wood`.

---

## Sector benchmarking · Data sources

### Sector-relative benchmarking

Every signal (screening / post-market anomaly / intraday) is compared against a sector ETF before push. `NVDA +5%` means different things when SMH is `+4.5%` (beta) vs `-1%` (ticker-specific). Mapping: semis (NVDA/AMD/AVGO/QCOM/INTC/TXN/MU) → SMH; mega-cap tech + software + internet (AAPL/MSFT/GOOGL/META/ORCL/CRM/ADBE, …) → XLK; default fallback → SPY. Pre-fetched once per agent run (3 extra FD calls). `contrarian` flag fires when ticker + sector move opposite with ≥1.5pp gap — only then does `★ 逆势` chip appear. Live example: `MU +3.63% with SMH -1.10% on 1.5× volume → ★ 逆势 + Tier-1 attribution "MU 市值突破 $1T"`.

### External data sources (7)

| Source | What | Why |
|---|---|---|
| **yfinance** | Daily OHLCV + options + macro VIX/DXY/WTI/Gold | Real-time EOD (no FD 1-3 day lag); free |
| **financialdatasets.ai** | Earnings / insider / fundamentals | Quarterly + episodic data; commercial API |
| **SEC EDGAR** (edgartools) | 13F-HR + 8-K + Form 4 + 10-Q | Authoritative |
| **ARK Invest CDN** | Daily ETF holdings CSV | Public; daily granularity |
| **Alpaca** (paper) | Real-time prices + account + P&L | Free; drives streamer + portfolio cron |
| **FRED** | CPI/PCE/NFP/GDP/PPI/Treasury yields/Fed Funds | Authoritative EOD; vintage handling; avoids yfinance `^TNX` × 10 raw bug |
| **Tavily News API** | News search + FOMC sell-side aggregate | Search-engine quality; Layer 3 defense |

LLM layer: **DeepSeek** (`deepseek-chat`) for generation + verification + intent classification; **OpenAI** `text-embedding-3-small` for RAG embeddings.

**Price-source decoupling**: `v2/data/price_source.py` exposes `PriceSource` Protocol + `YFinancePriceSource` (default) + `FDPriceSource` (backtest reproducibility) + `default_price_source()` factory. Ops can flip to FD via `V2_PRICE_SOURCE=fd` env var without redeploying.

---

## Project layout · Tech stack

**Tech stack**: Python 3.11 + Poetry; LangChain + `langchain-deepseek`; APScheduler `BlockingScheduler` + `US/Eastern` cron; python-telegram-bot (async polling); alpaca-py; SQLite WAL; ChromaDB; edgartools; matplotlib + Noto Sans CJK; systemd (Ubuntu 24.04).

```
v2/
├── data/  screening/  monitoring/  lateral/  institutional/   # ⓪–④
├── etf/                            # ⑤⑬ ARK CSV + snapshot + diff + alerts classifier
├── earnings/                       # ⑦⑧ yfinance + FD + LLM summary + 10-Q parser
├── portfolio/                      # ⑨⑨b⑩ Alpaca → RiskReport + positions_snapshot + attribution
├── sec/                            # ⑪⑫⑫b 8-K + Form 4 + cluster + 5.02 NER + insider digest
├── macro/                          # ⑭⑮⑯⑰ FRED + yfinance + Tavily + fomc_parser
├── streamer/  broker/  universe/   # intraday + Alpaca adapter + TECH_30 + 90-ticker sector mapping
├── reporting/                      # formatters + notifier + priority + 8 4-layer shims
├── memory/  archive/  bot/  scheduler/  observability/         # ChromaDB + SQLite log + bot + APScheduler + trace SDK

scripts/  (21 cron + 3 service entrypoints — names match the time table above)
```

---

## Quick start · Deployment

```bash
git clone <repo-url> hedge-fund
cd hedge-fund
poetry install --no-root

cp .env.example .env
# Fill: DEEPSEEK_API_KEY, FINANCIAL_DATASETS_API_KEY, TAVILY_API_KEY,
#       OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#       APCA_API_KEY_ID, APCA_API_SECRET_KEY   (Alpaca paper is free)

# Smoke-test one agent
poetry run python scripts/daily_screen_to_telegram.py

# Run scheduler (optional --test fires every job once and exits)
poetry run python scripts/run_scheduler.py
poetry run python scripts/run_telegram_bot.py
poetry run python scripts/run_streamer.py  # --test-now bypasses market-hours
```

**Production deployment (Ubuntu 24.04 + systemd)** — three service units of the same shape (replace paths/user, then `systemctl daemon-reload && systemctl enable --now hedge-fund-{scheduler,bot,streamer}`):

```ini
# /etc/systemd/system/hedge-fund-scheduler.service
[Unit]
Description=AI Hedge Fund Scheduler
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hedge-fund
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/root/hedge-fund"
ExecStart=/root/.local/bin/poetry run python scripts/run_scheduler.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/root/hedge-fund/logs/scheduler.log
StandardError=append:/root/hedge-fund/logs/scheduler.err

[Install]
WantedBy=multi-user.target
```

`hedge-fund-bot.service` and `hedge-fund-streamer.service` follow the same structure (swap script + log paths).

**Resource footprint**: scheduler ~205 MB, bot ~50 MB, streamer ~80 MB. **Runs comfortably on a 1 GB DigitalOcean droplet.**

---

## Testing

**474 sandbox unit tests, organized by family**:

| Family | Count | Coverage |
|---|---|---|
| Earnings (⑦⑧) | **57** | byte-equal 21 / priority ladder 17 / pipeline 9 / cron integration 10 |
| Portfolio (⑨⑨b⑩) | **79** | smoke 20 / priority ladder 19 + floor 5 / pipeline edges 10 / byte-equal 12 / cron integration 17 / ⑨b cron 6 |
| SEC (⑪⑫⑫b + 10-Q) | **131** | smoke 21+20 / priority 21 / byte-equal 15+5 / HTML safety 8 / cron integration 20+10 / bot responder 11 |
| Macro (⑭⑮⑯⑰) | **112** | smoke 40 / priority 16 / byte-equal 16 / HTML safety 9 / bot responder 9 / cron integration 22 |
| ARK (⑬) | **35** | smoke 16 / byte-equal + HTML + shim 11 / cron integration 8 |
| Cross-cutting | **60+** | archive migration 9 / intent classify 33 / base priority 18 / observability 13 |

**Architecture guard tests**: regression tests pin `v2/bot/responders.py` + `scripts/*_to_telegram.py` to forbid inline `_format_*` private helpers. All crons must `from v2.reporting import` via public API. Prevents future "convenient inline" rollbacks from undoing the lift-and-shift work.

**Byte-equal pin** (24+ cases across 8 formatter families): every public formatter (`format_earnings_*` / `format_portfolio_*` / `format_sec_*` / `format_macro_*` / `format_ark_*`) is locked byte-equal under multiple fixture × case combos. Cron push == bot response == formatter output.

All 474 tests pass under `pytest` in the sandbox environment with no v2.data deps required (production-only deps are stubbed via sys.modules).

---

## Engineering highlights

**Push vs pull semantics separated** — `/13f BRK` always returns the latest portfolio regardless of scheduler push history. Cron dedupes against `edgar.db`; bot path skips that check. One pipeline, two interfaces. Streamer follows the same idea (user `/alert` and TECH_30 auto-scan are independent code paths sharing the loop).

**CUSIP aggregation in 13F parsing** — Berkshire holds AAPL across three subsidiaries (BHRG, GEICO, National Indemnity) as separate rows with identical CUSIP. Naive `INSERT OR REPLACE` would silently drop two of three. We aggregate at the EDGAR parser layer. **Berkshire's AAPL stake reads $57.8B (22%) instead of the wrong $958M of whichever fragment landed last.**

**Strict-enum intent classification** — 23 intents, JSON output validated against whitelist set, anything else → `unknown`. **The LLM never decides what to *say*, only which tool to *call*.** The point of this system is grounded, multi-source, verified output.

**Atomic alert firing + sector-benchmark moat** — streamer's `alert_fire_check` runs `UPDATE alerts SET fired_at=? WHERE id=? AND fired_at IS NULL`; one-shot semantics enforced at SQL layer. Pre-fetched 3 ETF series (SPY/XLK/SMH) per agent run give every anomaly a relative-strength chip without LLM tokens.

**Source-tier defense + stale-data chip** — Verifier scores Tier 1/2/3; Generator only sees Tier 1+2. Silent omission > confident misinformation. Greenlight's latest 13F is 2023-Q4; without ⚠️ chip we'd silently misrepresent 29-month-old positions as Einhorn's current state.

**Intraday streamer deliberately runs no LLM** — during the regular session no Tavily, no DeepSeek, no news layer. Intraday news lags the move; firing LLM attribution at 10:15 on a 10:14 spike just produces fabrication.

**8 4-layer formatter shims keep cross-surface byte-equal** — `format_*` source-of-truth in `v2/{earnings,portfolio,sec,macro,etf}/_bot_cards.py` (sandbox-runnable), re-exported via `v2/reporting/_*_formatters.py`. Cron + bot share the same function objects (identity-checked by tests).

**Stage 0 audit + silent-ship defense** — Phase 5a Stage 0 audit found v2/etf/ already covered the ARK pipeline → scope pivot **saved 87% of code / 31% of hours** (~1500 → ~250 lines). Phase 3.5 Stage 4 cron-integration tests **caught a Stage 2 silent-ship bug** — priority.py added `+20 going_concern_in_10q` but the cron never forwarded the flag into metadata. End-to-end tests are the only defense. Phase 5a Stage 4 mirrors the pattern: asserts literal `multi_fund_coordination` / `held_or_watchlist_ark` strings in `priority_reasons` trail.

---

## Cost analysis

DigitalOcean 1 GB droplet **$6.00** + financialdatasets.ai cached **~$15** + DeepSeek ~3M tokens/mo **~$1** + OpenAI embeddings ~50K dims/mo **~$0.10** + Tavily ~500/mo **$0 (free tier)** + Alpaca paper IEX **$0** = **~$22/month total**. `CachedFDClient` eliminates ~65% of FD calls via per-endpoint TTL SQLite cache (24h fundamentals, 6h prices, 7d company facts; news deliberately uncached). Intraday streamer adds ~30 Alpaca calls/min during market hours, well under the 200/min free-tier limit.

---

## Roadmap

### ✅ Shipped (Phase 0–5a)

| Phase | Description |
|---|---|
| **Phase 0** | Push priority system (P0/P1/P2/P3 + importance_score + P2 digest cron) |
| **Phase 1** | Earnings Agent (⑦⑧ + `/earnings`) |
| **Phase 2** | Portfolio Risk Agent (⑨⑩ + `/risk` + `/pnl`) |
| **Phase 2.5-mini** | BROAD-market ETF bucket (IVV/SPY/VOO/QQQ classification) |
| **Phase 2.5 full** | Per-position attribution (⑨b 16:25 ET sub-cron + `positions_snapshot` + ⑩ Brinson-style) + drawdown realtime fix (2026-06-05 prod bug) |
| **Phase 3** | SEC Monitoring (⑪⑫ + `/8k` + `/insiders`) + 24-item priority table + Form 4 noise/signal + ≥3 cluster + 5.02 LLM NER |
| **Phase 3.5** | 10-Q parser + ⑧ MD&A diff (going_concern P0 / material_weakness +15) + ⑫b Insider Weekly Digest (Fri 19:15 ET, title-only) |
| **Phase 4** | Macro Agent (⑭⑮⑯⑰ + `/macro` + `/cpi` + `/fomc` + `/yields`) + FRED+yfinance hybrid + LLM 4-layer defense |
| **Phase 4.5-mini** | FD → yfinance daily-prices migration (`PriceSource` Protocol + `V2_PRICE_SOURCE=fd` fallback) |
| **Phase 5a** | ⑬ ARK Rebalance Alerts (Mon-Fri 08:30 ET) + multi-fund coordination + reuses v2/etf/ |
| **Cross-cutting** | Web dashboard (FastAPI + React + Tailwind + trace replay); observability SDK |

### ⏳ Deferred — waiting on real data / trigger conditions

> **Naming clarification**: Phase 4.5-mini ✅ shipped (FD → yfinance); Phase 4.5 ⏳ deferred (FOMC transcript + Historical analog).

**⏳ Phase 3.5.5 — Form 4 structured table**

`form4_transactions` table replacing ⑫b's title-only fallback: per-A/M/F/G/C breakdown + aggregate USD + per-insider trends. **Trigger**: ⑫b weekly digest live for 1+ months with operator feedback on per-code breakdown needs.

**⏳ Phase 4.5 — FOMC Transcript + Historical Analog (LLM Layer 4)**

Task A: ⑮b FOMC transcript +6h follow-up (Bloomberg/Reuters Q&A → hawkish/dovish phrase count). Task B: Historical-analog mode (Layer 4 defense — LLM cites past N similar σ-surprise events + SPX 5-day returns as guardrail). **Trigger**: 2026-06-17 FOMC + SEP completed; Phase 4 ⑮⑰ live ≥ 2 months; operator has Layer 1+2 feedback. **Effort**: 1.5-2 days · **Risk**: medium.

**⏳ Phase 5b — Market Regime Detection**

Synthesize VIX + 10Y-2Y + breadth + sentiment → regime label (risk_on / risk_off / transition / extreme_vol). New ⑭b cron Mon-Fri 16:35 ET; regime transitions → P0 alerts; per-regime holdings attribution (with ⑨b snapshot). **Trigger**: Phase 4 full suite live ≥ 4 weeks; Phase 4.5 shipped; operator has macro-signal scale. **Effort**: 2-3 days · **Risk**: high (regime detection is overfit-prone).

**⏳ Phase 6 — News Refactor + 3-tier Universe**

Task A: central Tavily query gateway + per-ticker cache + NER ticker mapping. Task B: 3-tier Universe (core / watchlist / broad, each with different cron frequency). **Trigger** (any one): universe ≥ 30 tickers; OR monthly LLM cost > $50; OR ≥ 5% pushes are Tier-3/4 noise. **Effort**: 4 days · **Risk**: medium.

### Ongoing / pending

Migration system audit; GitHub Actions CI (pytest on main); multi-user support; backtesting integration; Alpaca paper→live switch.

> Currently in the production observation phase — focus is validating Phase 0–5a under real-world data.

---

## Credits · License

The repository's outer layout and the original educational `app/` directory come from [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), an open-source AI hedge fund concept project. The `v2/` directory — the entire alternative-data agent system described in this README (21 cron jobs, intraday streamer, Telegram bot with 23 NL intents, 5 hallucination-defense mechanisms, push-priority system, all SQLite + ChromaDB stores, all systemd deployment scaffolding) — was built from scratch as a separate project on top of that foundation.

License: **MIT**, educational project, **not investment advice**.
