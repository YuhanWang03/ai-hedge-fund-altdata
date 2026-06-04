# AI Hedge Fund · Alternative Data Agent System

A production-grade alternative-data intelligence platform delivered through a Telegram bot. Six post-market batch agents continuously monitor US equity markets, two earnings crons track watchlist + holdings for D-3/D-1/D-0 reminders and post-release summaries, two portfolio risk crons push daily concentration / P&L / drawdown snapshots plus a Friday weekly recap, one intraday streamer scans for live anomalies and price-alert triggers, nineteen natural-language intents handle interactive queries, and five distinct hallucination-defense mechanisms keep every claim grounded in primary sources.

Built as a portfolio project to demonstrate end-to-end ownership of a multi-source data pipeline, dual-LLM verification architecture, and 24/7 deployed system.

[中文版 README](./README_zh.md)

---

## What it does

The system runs three concurrent services on a single $6/month VPS:

**Scheduler** — twelve cron jobs covering screening, anomaly detection, supply-chain expansion, institutional 13F tracking, weekly backfills, daily ETF holdings ingestion, earnings reminders, post-release earnings summaries, portfolio risk snapshot, portfolio weekly recap, P2 daily digest, and archive sweep. Each job is process-isolated; one crashing cannot poison the others.

**Streamer** — minute-level intraday scanner. Polls Alpaca's real-time feed during US market hours (9:30 - 16:00 ET) and does two things: checks user-set price alerts for crossings, and scans the TECH_30 universe for dual-threshold anomalies (≥3% price move AND ≥2.5× volume pace). Deliberately runs no LLM/Tavily during market hours — deep attribution happens at 17:35 ET.

**Telegram Bot** — always-on long-polling bot supporting nineteen slash commands, three watchlist commands, and a natural-language intent classifier with nineteen canonical intents. Authorized to a single user (chat ID filter).

All three services share seven SQLite databases via WAL mode and a ChromaDB vector store for RAG memory.

---

## System architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-scheduler.service                 │
├──────────────────────────────────────────────────────────────────┤
│  08:00 ET Mon-Fri  ⑦ Earnings Reminders        → Telegram push   │
│  17:00 ET Mon-Fri  ⑤ ETF Daily Snapshot       (silent ingest)    │
│  17:30 ET Mon-Fri  ① Daily Screen              → Telegram push   │
│  17:35 ET Mon-Fri  ② Anomaly Monitor           → Telegram push   │
│  18:00 ET Mon      ③ Lateral Expansion         → Telegram push   │
│  18:00 ET Tue/Fri  ④ Institutional 13F         → Telegram push   │
│  18:30 ET Mon-Fri  ⑨ Portfolio Risk Snapshot   → Telegram push   │
│  18:30 ET Sun      ④b 13F Backfill             (silent refresh)  │
│  19:00 ET Fri      ⑩ Portfolio Weekly Recap    → Telegram push   │
│  21:00 ET Mon-Fri  ⑧ Earnings Summaries        → Telegram push   │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                     hedge-fund-streamer.service                  │
├──────────────────────────────────────────────────────────────────┤
│  9:30 - 16:00 ET Mon-Fri · every 60s                             │
│   ├─ User /alert triggers     · Alpaca latest-trade × ticker     │
│   └─ TECH_30 auto-scan        · dual-threshold + sector contra   │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Shared SQLite + ChromaDB                       │
│  archive.db     fd_cache.db     edgar.db     etf.db              │
│  bot_state.db   options.db      chroma/      screening_cache.db  │
└──────────────────────────────────────────────────────────────────┘
                              ▲
                              │
┌──────────────────────────────────────────────────────────────────┐
│                      hedge-fund-bot.service                      │
├──────────────────────────────────────────────────────────────────┤
│  Slash commands · /why /summary /chain /13f /holders /etf        │
│                   /alert /alerts /alert_remove                   │
│                   /portfolio /pnl /settings                      │
│                   /watchlist /add /remove                        │
│  NL classifier  · 15 intents · DeepSeek T=0 · strict-enum        │
└──────────────────────────────────────────────────────────────────┘

External: financialdatasets.ai · yfinance · SEC EDGAR · ARK CSV CDN
          Alpaca · Tavily News API · DeepSeek LLM · OpenAI embeddings
```

---

## The six scheduled agents

### ① Daily Screen (Mon-Fri 17:30 ET)

Fundamental screening of a TECH_30 universe against hard rules (market cap, revenue growth, gross margin, volatility), enriched with nine qualitative tags (高成长, 高毛利, 小盘, 业绩超预期, etc.). Passes get LLM narration in Template-Fill mode — the model outputs only qualitative logic while Python injects all numbers, eliminating numeric hallucination.

Each candidate is augmented with **sector-relative strength** vs its mapped sector ETF (semis → SMH, broader tech → XLK), surfacing leadership patterns like *领涨 / 掉队 / 同步* that single-stock metrics miss.

Survivors receive recent Tavily news headlines and peer-relative performance deltas for context.

### ② Anomaly Monitor (Mon-Fri 17:35 ET)

Detects volume spikes (≥3× 30-day average), 52-week highs/lows, and significant insider buy/sell clusters across the TECH_30 universe. Each detected anomaly:

1. Gets a **sector-relative chip** (`★ 逆势上涨` when ticker and sector move in opposite directions with ≥1.5pp gap)
2. Fires a multi-source attribution pipeline:
   - Tavily news search filtered by entity match (ticker, company name, executives)
   - Verifier LLM scores each source by tier (SEC/Reuters/Bloomberg = Tier 1, Investors/SeekingAlpha = Tier 2, others = Tier 3)
   - Generator LLM synthesizes attribution using only Tier-1 + Tier-2 sources
3. Result persisted to ChromaDB with deterministic ID `{ticker}_{date}` for RAG retrieval

### ③ Lateral Expansion (Mon 18:00 ET)

Takes the previous day's anomaly seeds and asks DeepSeek to propose supply-chain neighbors (suppliers, customers, smaller peers, beneficiaries). Each proposed relation is then verified by a follow-up Tavily search requiring co-occurrence of seed + neighbor tickers in news content — unverified relations are dropped.

Verified neighbors are filtered against market cap / revenue growth / margin thresholds before being pushed.

### ④ Institutional 13F (Tue/Fri 18:00 ET)

Pulls fresh 13F-HR filings from SEC EDGAR for ten famous managers (Berkshire, Burry, Ackman, Einhorn, Renaissance, Two Sigma, D.E. Shaw, Citadel, Coatue, ARK). For each new filing, computes quarter-over-quarter position changes, classifies as 新进 / 加仓 / 减仓 / 清仓, sends top-20 changes to DeepSeek for ten-word strategic interpretations.

Currently tracks **$1.29T AUM across 17,329 positions**.

### ④b 13F Backfill (Sun 18:30 ET)

Weekly safety net that re-fetches all ten managers' latest two filings, idempotently overwriting positions with the latest CUSIP-aggregated values. Catches amended filings, transient EDGAR errors, and race conditions the Tue/Fri push agent might have missed.

### ⑤ ETF Daily Snapshot (Mon-Fri 17:00 ET)

Silently fetches daily holdings CSVs for four ARK funds (ARKK, ARKW, ARKG, ARKF) from ARK Invest's public CDN. Builds a per-(fund, date, ticker) time series enabling 24-hour rebalance detection — daily granularity vs the 45-day-delayed 13F.

---

## The intraday streamer service

Runs in parallel to the scheduler. Polls every 60 seconds during the regular session (9:30 - 16:00 ET, Mon-Fri); sleeps in 5-minute windows outside trading hours.

Each poll does two independent jobs:

### A. User-set price alerts

Users create alerts via `/alert NVDA 130 above` or natural language ("提醒我 NVDA 突破 130"). Each minute, the streamer:

1. Queries the `alerts` table for unfired entries
2. Batches the distinct tickers into one Alpaca `get_stock_latest_trade` request
3. For each ticker, runs `alert_fire_check(ticker, current_price)` which **atomically marks** any crossed alerts (`UPDATE … WHERE fired_at IS NULL`) and returns them
4. Pushes a triggered card per fired alert; never re-fires (one-shot semantics by design)

### B. TECH_30 automated anomaly scan

Every minute, also scans all 29 TECH_30 tickers for intraday anomalies:

- **Dual threshold**: ≥3% move from open AND ≥2.5× volume pace
- **Volume pace** = `today_volume / (avg_30d × market_progress)` — normalizes for partial session
- **30d avg volume** baseline auto-refreshes every 7 trading days via Alpaca daily bars
- **Sector-relative context** included automatically (`★ 逆势` chip when contrarian)
- **30-minute per-ticker cooldown** via `intraday_cooldown` table prevents spam
- **Deliberately no LLM** during market hours — fast signals during session, deep attribution at 17:35 ET

A typical fire looks like:

```
⚡ 盘中异动 · IBM · 10:15 ET
━━━━━━━━━━━━━━━━━━━━
📈 现价 $297.97  +7.45%  vs 开盘 $277.30
当日成交 28.4M  ·  节奏 3.6×  (已交易 11% 时段)
对比 XLK +0.40%  ·  差 ↑+7.05pp ★ 逆势

用 /why IBM 看盘后完整归因（17:35 ET 起效）
```

---

## Telegram interface

The bot's natural-language layer classifies free-form Chinese or English text into nineteen canonical intents using DeepSeek at temperature=0. Outputs are validated against a closed enum whitelist — unrecognized intents fall back to `unknown`, guaranteeing bounded behavior.

### Slash commands

| Category | Command | What it does |
|---|---|---|
| Watchlist | `/watchlist`, `/add NVDA`, `/remove TSLA` | Manage personal ticker list |
| Analysis | `/why TICKER` | On-demand anomaly attribution |
| | `/summary TICKER` | Multi-dimensional snapshot: price, fundamentals, earnings, insiders, news |
| | `/chain TICKER` | Lateral supply-chain expansion |
| | `/13f MANAGER` | Full portfolio snapshot + QoQ changes |
| | `/holders TICKER` | Reverse query: which tracked managers hold this ticker |
| | `/etf SYMBOL` | ARK fund daily holdings + 24h rebalance |
| | `/earnings AAPL` | Single-ticker earnings card (next release + last filing + ⭐ chips) |
| | `/earnings` | 14-day forward calendar across watchlist ∪ holdings |
| Alerts | `/alert NVDA 130 above` | Create a price-threshold alert |
| | `/alerts` | List unfired alerts |
| | `/alert_remove ID` | Delete an alert |
| Account | `/portfolio` | Alpaca holdings + cash |
| | `/pnl [day\|week\|month]` | Today's P&L (default `day` = Phase 0 behavior); `week` / `month` use ⑨⑩ data |
| | `/risk` | Real-time portfolio risk snapshot (same as ⑨ cron, read-only) |
| Meta | `/settings`, `/help`, `/start` | Settings + command reference |

### Natural-language examples

| User input | Routed to |
|---|---|
| `NVDA 为什么跌？` | `explain_move` · NVDA |
| `看看 AAPL 怎么样` | `summary` · AAPL |
| `找一下 AMD 的产业链` | `chain` · AMD |
| `巴菲特最近买了什么` | `thirteen_f` · brk |
| `谁持有 NVDA` | `holders_view` · NVDA |
| `Cathie 今天买啥` | `etf_view` · ARKK |
| `提醒我 NVDA 突破 130` | `alert_set` · NVDA · 130 · above |
| `我设了哪些提醒` | `alert_list` |
| `看看 Alpaca 持仓` | `portfolio_view` |
| `我的当日盈亏` | `pnl_view` |
| `我关注了哪些股票` | `watchlist_view` |
| `最近有什么异动` | `find_anomalies` |
| `今天天气怎么样` | `unknown` |

### Manager aliases (10 supported)

`brk / berkshire / buffett`, `burry / scion`, `ackman / pershing`, `einhorn / greenlight`, `renaissance / rentech`, `twosigma`, `deshaw / shaw`, `citadel`, `coatue`, `ark / cathie / wood`

---

## Hallucination defense — five layers

LLMs in this system never produce numeric facts directly. They make classification decisions, write qualitative narration, and select between candidate sources. All numbers come from primary data sources.

| Layer | Mechanism |
|---|---|
| 1. Entity filter | News results must contain the target ticker AND at least one company-name token before being shown to the Verifier |
| 2. Source-tier scoring | Verifier LLM scores each source as Tier 1 (SEC, Reuters, Bloomberg, WSJ, FT, CNBC) / Tier 2 (MarketWatch, SeekingAlpha, Yahoo, Fool, Forbes) / Tier 3 (others). Tier 3 is discarded |
| 3. Generator-Verifier split | A separate Verifier LLM call validates the Generator's output against the source list, with strict prompt rules forbidding numbers not present in input |
| 4. Template Fill | Screening narrator outputs only qualitative phrases; Python f-string injects all numbers from the data layer. LLM cannot misread a metric |
| 5. Stale-data chip | Filings older than 180 days show ⚠️; older than 365 days show ⚠️ 已 N 月未更新. Prevents users from interpreting 2023 13F data as current state |

---

## Sector-relative benchmarking

Every signal — screening, post-market anomaly, intraday anomaly — is compared against a sector ETF benchmark before being surfaced. A `+5%` volume spike on NVDA means something very different when SMH is `+4.5%` (market beta) versus when SMH is `-1%` (ticker-specific catalyst).

| Ticker family | Sector ETF |
|---|---|
| Semiconductors (NVDA, AMD, AVGO, QCOM, INTC, TXN, MU) | SMH |
| Mega-cap tech + software + internet (AAPL, MSFT, GOOGL, META, ORCL, CRM, ADBE, …) | XLK |
| Default fallback | SPY |

Sector ETF prices are pre-fetched once per agent run (3 extra FD calls for the whole universe). The `contrarian` flag fires when ticker and sector move in opposite directions with ≥1.5pp gap — only then does the `★ 逆势` chip appear.

Live signal example from production: `MU +3.63% with SMH -1.10% on 1.5× volume → ★ 逆势 chip + Tier-1 attribution surfacing "MU 市值突破 $1T" as ticker-specific catalyst.`

---

## Data sources

| Source | What | Why this |
|---|---|---|
| financialdatasets.ai | Fundamentals, prices, insider trades, earnings | Primary commercial API; consistent schema |
| yfinance | Fallback prices, options chains (OI + volume) | Free; what powers the unusual-options-activity detector |
| SEC EDGAR (via `edgartools`) | 13F-HR institutional filings | Authoritative source for fund holdings |
| ARK Invest CDN | Daily ETF holdings CSV | Public; daily granularity |
| Alpaca (paper) | Real-time prices, account portfolio + P&L | Free; powers `/portfolio`, `/pnl`, streamer alerts |
| Tavily News API | News search for attribution + verification | Search-engine quality without scraping |
| DeepSeek (`deepseek-chat`) | Generation + verification + intent classification | Best price/performance for Chinese + English |
| OpenAI `text-embedding-3-small` | RAG memory embeddings | Cheap, accurate; small dimension |

---

## Tech stack

- **Python 3.11** + Poetry
- **LangChain** + `langchain-deepseek` for LLM orchestration
- **APScheduler** with `BlockingScheduler` and cron triggers in `US/Eastern`
- **python-telegram-bot** (async, polling mode)
- **alpaca-py** for real-time prices + paper account access
- **SQLite** with WAL journal mode for concurrent read+write across services
- **ChromaDB** for vector-backed long-term memory
- **edgartools** for SEC filings
- **matplotlib** with Noto Sans CJK for chart rendering
- **systemd** for service supervision on Ubuntu 24.04

---

## Project layout

```
v2/
├── data/             # FDClient + CachedFDClient + NewsProvider Protocol
├── screening/        # ① fundamental screen + delta enricher + narrator
├── monitoring/       # ② detector + multi-source attributor + Verifier
├── lateral/          # ③ LLM expansion + Tavily relation verification
├── institutional/    # ④ EDGAR client + CUSIP aggregation + changes
├── etf/              # ⑤ ARK CSV client + daily snapshot store + diff
├── streamer/         # intraday runner + universe scanner
├── broker/           # Alpaca paper-account adapter (read-only)
├── universe/         # TECH_30 list + sector ETF mapping
├── reporting/        # Telegram formatters + notifier + matplotlib charts
├── memory/           # ChromaDB-backed AnomalyMemory
├── archive/          # SQLite log of every push for offline query + RAG
├── bot/              # Telegram bot: commands, intent classifier, responders, state
└── scheduler/        # APScheduler config + job subprocesses

scripts/
├── run_scheduler.py             # Scheduler entrypoint
├── run_telegram_bot.py          # Bot entrypoint
├── run_streamer.py              # Streamer entrypoint (--test-now forces one scan)
├── daily_screen_to_telegram.py  # ①
├── anomaly_to_telegram.py       # ②
├── lateral_to_telegram.py       # ③
├── institutional_to_telegram.py # ④
├── backfill_13f.py              # ④b
└── etf_daily_snapshot.py        # ⑤
```

---

## Quick start

### Prerequisites

- Python 3.11
- Poetry
- API keys: DeepSeek, financialdatasets.ai, Tavily, OpenAI, Telegram bot token, Alpaca (paper)

### Setup

```bash
git clone <repo-url> hedge-fund
cd hedge-fund
poetry install --no-root

cp .env.example .env
# Fill in:
#   DEEPSEEK_API_KEY, FINANCIAL_DATASETS_API_KEY, TAVILY_API_KEY,
#   OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#   APCA_API_KEY_ID, APCA_API_SECRET_KEY   (Alpaca — paper account is free)
```

### Smoke-test one agent

```bash
poetry run python scripts/daily_screen_to_telegram.py
```

### Run the scheduler (foreground)

```bash
poetry run python scripts/run_scheduler.py
# Optional: --test  to fire every job once and exit
```

### Run the bot

```bash
poetry run python scripts/run_telegram_bot.py
```

### Run the streamer

```bash
poetry run python scripts/run_streamer.py
# Optional: --test-now  to force ONE poll regardless of market hours,
#           push results to Telegram, then exit
```

---

## Deployment (Ubuntu 24.04, systemd)

Three services run concurrently. Sample unit files:

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

```ini
# /etc/systemd/system/hedge-fund-bot.service
[Unit]
Description=AI Hedge Fund Telegram Bot
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hedge-fund
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/root/hedge-fund"
ExecStart=/root/.local/bin/poetry run python scripts/run_telegram_bot.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/root/hedge-fund/logs/bot.log
StandardError=append:/root/hedge-fund/logs/bot.err

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/hedge-fund-streamer.service
[Unit]
Description=AI Hedge Fund Alert Streamer
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hedge-fund
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/root/hedge-fund"
ExecStart=/root/.local/bin/poetry run python scripts/run_streamer.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/root/hedge-fund/logs/streamer.log
StandardError=append:/root/hedge-fund/logs/streamer.err

[Install]
WantedBy=multi-user.target
```

Combined resident memory: scheduler ~205 MB, bot ~50 MB, streamer ~80 MB. Runs comfortably on a 1 GB DigitalOcean droplet.

---

## Engineering notes worth highlighting

### Push vs pull semantics

`/13f BRK` always returns the latest known portfolio, regardless of whether the same filing has already been pushed by the scheduler. The cron path dedupes against `edgar.db` to avoid duplicate alerts; the bot path skips that check entirely. Two distinct invocations of the same underlying pipeline, one for each access pattern.

### CUSIP aggregation in 13F parsing

Berkshire reports AAPL across three subsidiaries (BHRG, GEICO, National Indemnity). The 13F-HR information table lists them as separate rows with identical CUSIP. The naive PK `(accession, cusip)` would silently drop two of the three via `INSERT OR REPLACE`. We aggregate at the EDGAR parser layer instead — one row per CUSIP per accession, summed shares and market value. Berkshire's AAPL stake reads $57.8B (22% of portfolio), not the $958M of whichever fragment landed last.

### Strict-enum intent classification

The NL classifier outputs one of nineteen intents plus arguments. Output is JSON, parsed, validated against a whitelist set. Anything else becomes `unknown`. The LLM never decides what to *say* — only which tool to *call*. If users wanted DeepSeek to write financial analysis from its imagination, they could ask DeepSeek directly. The point of this system is grounded, multi-source, verified output.

### Atomic alert firing

Streamer's `alert_fire_check` runs `UPDATE alerts SET fired_at=? WHERE id=? AND fired_at IS NULL`. The `fired_at IS NULL` guard makes the operation idempotent under concurrent streamer instances — even if two pollers raced for the same alert, only the first would claim it. One-shot alert semantics enforced at the SQL layer rather than in application code.

### Push and pull on the streamer too

A user `/alert` and a TECH_30 auto-scan are independent code paths sharing the streamer loop. A failure in one doesn't suppress the other — `_check_user_alerts` and `scan_universe` are both wrapped in try/except. The streamer never dies because of one bad ticker; the worst case is that ticker silently skipped this minute.

### Sector benchmarking as a signal-quality moat

Pre-fetching 3 ETF series (SPY/XLK/SMH) per agent run gives every detected anomaly a relative-strength comparison without spending extra LLM tokens. The `★ 逆势` chip surfaces signals like *NVDA +3% on a day SMH is -1%* that the single-stock pipeline would file alongside true-beta noise like *AAPL +1.2% on a +1.0% market day*.

### Async-safe long-running operations

Bot commands like `/why NVDA` trigger 20-second pipelines (FD calls + Tavily + DeepSeek × 2). Wrapped via `asyncio.get_running_loop().run_in_executor(None, sync_fn, *args)` so the polling loop stays responsive to other users (it doesn't matter for single-user mode, but the pattern is correct).

### Source-tier defense in attribution

The Verifier prompt enumerates Tier-1 / Tier-2 / Tier-3 domains. The Generator only sees Tier-1 + Tier-2 sources. Avoiding pollution from speculation sites was an explicit design choice — silent omission beats confident misinformation.

### Stale data is data integrity

Greenlight Capital's latest 13F is 2023-Q4. They likely fell below the $100M reporting threshold. Showing this data without a ⚠️ chip would silently misrepresent 29-month-old positions as Einhorn's current state. The chip surfaces the temporal boundary explicitly.

### Intraday scanner deliberately runs no LLM

During regular session hours, the streamer scans TECH_30 every minute for dual-threshold anomalies and pushes a simplified card. **It does not call Tavily, does not call DeepSeek, does not even pull the news layer.** Intraday news lags the move; if you fire LLM attribution at 10:15 on a 10:14 spike, the model has nothing real to work with and will fabricate. Instead, the intraday card explicitly points to `/why TICKER` and notes that deep attribution becomes available at 17:35 ET when the post-market Anomaly Monitor runs.

---

## Cost analysis

Monthly operating cost on a real deployment:

| Item | Cost |
|---|---|
| DigitalOcean droplet (1 GB) | $6.00 |
| financialdatasets.ai (cached) | ~$15 |
| DeepSeek tokens (~3M/mo across agents + bot) | ~$1 |
| OpenAI embeddings (~50K dims/mo) | ~$0.10 |
| Tavily News (~500 queries/mo) | $0 (free tier) |
| Alpaca (paper account, IEX data) | $0 |
| **Total** | **~$22/month** |

The CachedFDClient layer eliminates roughly 65% of FD calls by routing per-endpoint queries through a SQLite TTL cache (24h for fundamentals, 6h for prices, 7d for company facts; news deliberately never cached).

Intraday streamer adds ~30 Alpaca API calls/minute during market hours (one batched latest-trade + one batched day-bars call covering all 32 tracked symbols), well under the 200/min free-tier limit.

---

## Roadmap

### Shipped ✅

- **Phase 0 · Push priority system** — `v2/reporting/priority.py` (P0/P1/P2/P3 + importance_score) plus 16:45 ET P2 digest cron
- **Phase 1 · Earnings Agent (⑦⑧ + /earnings)** — yfinance forward calendar + FD actual/estimate + LLM template-fill summary + Tavily transcript + cross-quarter dedup
- **Phase 2 · Portfolio Risk Agent (⑨⑩ + /risk + /pnl extension)** — daily concentration / P&L / drawdown / 7-day earnings risk, Friday weekly recap, 4 public formatters with byte-equal pins, drawdown 5-layer sign-convention defense
- **Web dashboard** — FastAPI + React + Tailwind over `archive.db` auto-push feed with trace replay and a user QA mode
- **Observability SDK** — `v2/observability/` monkey-patches FD / DeepSeek / Tavily / intent classifier so every agent produces a trace automatically
- **163 sandbox unit tests** across all three phases — 21 earnings byte-equal + 9 portfolio byte-equal + 17 earnings priority + 19 portfolio priority + 5 priority floor + 10 portfolio pipeline edges + 13 portfolio cron integration + 10 earnings cron integration + 9 earnings pipeline + 9 archive migration + 14 intent classify + 18 base priority + 13 observability + the rest across other modules

### In progress / TODO

- **Phase 2.5 (optional)** — per-position weekly attribution (daily `positions_snapshot` table + AM snapshot sub-cron)
- **Phase 3 · SEC monitoring** — 8-K / Form 4 / 10-Q real-time tracking (extends existing `edgartools` usage)
- **Phase 4 · Macro Agent** — FOMC / CPI / PCE / NFP via FRED + Tavily
- **Phase 5 · Market Regime + ARK significant-rebalance alerts**
- **Phase 6 · News pipeline refactor + 3-tier Universe**
- Ongoing: Migration system audit
- GitHub Actions CI (pytest on main)
- Multi-user support with per-chat watchlists + holdings
- Backtesting integration with the existing v2 event-study framework
- Switch Alpaca from paper to live (one config flag) after extended paper validation

---

## Credits

The repository's outer layout and the original educational `app/` directory come from [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), an open-source AI hedge fund concept project. The `v2/` directory — the entire alternative-data agent system described in this README (six post-market agents + two earnings crons + two portfolio risk crons, intraday streamer, Telegram bot with 19 NL intents, 5 hallucination-defense mechanisms, push-priority system, all SQLite + ChromaDB stores, all systemd deployment scaffolding) — was built from scratch as a separate project on top of that foundation.

## License

MIT. Educational project. Not investment advice.
