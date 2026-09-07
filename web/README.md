# AI Hedge Fund · Web (v2 rebuild)

Personal, single-user **operational panel + chat** — a fresh full-stack
rebuild. The old `dashboard/` (a trace-demo with guest/budget machinery) is
frozen; this replaces it.

- **Left** — operational panel: portfolio, risk, watchlist, latest signals,
  money-flow charts. *(Phase 2)*
- **Right** — chat: free-form NL, reusing the exact `v2` intents + responders
  the Telegram bot uses. *(Phase 1 — done)*

Backend is a **thin FastAPI layer over the existing `v2` modules** — it does
not reimplement any trading logic.

## Status

- ✅ **Phase 1** — backend skeleton + chat pipe (`/api/chat`)
- ✅ **Phase 2 (MVP)** — two-pane React frontend + `/api/portfolio` (equity /
  intraday P&L / positions / 1M equity sparkline). Chat renders responder
  cards + money-flow charts inline.
- ⬜ **Next** — more panel cards (risk / watchlist / signals feed), interactive
  money-flow charts, VPS deploy (systemd + nginx)

## Layout

```
web/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app + CORS
│   │   ├── config.py          # env settings (owner token, archive.db path)
│   │   ├── auth.py            # X-Owner-Token dependency (no-op if unset)
│   │   ├── dispatch.py        # intent → v2 responder (bot-identical cards)
│   │   └── routers/
│   │       ├── health.py      # GET /api/health
│   │       └── chat.py        # POST /api/chat
│   └── requirements.txt
└── frontend/                  # React + Vite + TS + Tailwind  (Phase 1.5)
```

## Run the backend (local)

```bash
cd web/backend
pip install -r requirements.txt            # or use the project's poetry env
WEB_OWNER_TOKEN=dev PYTHONPATH=.:../.. \
    uvicorn app.main:app --reload --port 8100
# → http://127.0.0.1:8100/api/health
```

Smoke-test the chat:

```bash
curl -s http://127.0.0.1:8100/api/chat \
  -H 'Content-Type: application/json' -H 'X-Owner-Token: dev' \
  -d '{"text":"微软资金流怎么样"}' | jq
```

## Run the frontend (local)

```bash
cd web/frontend
npm install
npm run dev          # → http://127.0.0.1:5173  (proxies /api to :8100)
```

Open the page, paste your `WEB_OWNER_TOKEN` into the header box (if auth is on),
click 保存. Left pane = portfolio; right pane = chat.

## Lab (实验室)

The Lab is one pipeline — 筛选 → 委员会 → 回测 → 观察 → 批准 — implemented
by deterministic engines behind `routers/workspace.py` and
`routers/committee.py`. Every run is persisted (`data/lab.db`, override with
`WEB_LAB_DB`) and can be reopened from 运行记录. Nothing here writes
production state; the only "approve" action is adding a ticker to the
watchlist.

All engines share the universe vocabulary in `app/sources.py`:
`custom` | `tech30` | `holdings` | `watchlist` | `holdings_watchlist`, plus the
index lists `sp500` | `nasdaq100` | `dow30` (screener only; bundled snapshot
in `v2/screening/universes.py`, refresh on the VPS with
`poetry run python -m v2.screening.universes --refresh`, which writes
`data/universes.json` from Wikipedia). A screen of more than 40 tickers runs
as a background job — `POST /api/lab/screening` returns `{job_id, done, total}`
and `GET /api/lab/screening/jobs/{id}` is polled — because nginx cuts requests
at 90 s. `GET /api/lab/universes` lists sizes and snapshot dates.

Backtest strategies (all in `v2/backtesting/strategies.py`, one engine):
`pead` (EPS beat/miss after the filing, `earnings_limit`), `momentum` (12-1
momentum ranked every `holding_days`, `lookback_days`, `skip_days`, `top_n`,
optional `near_high_pct` for the 52-week-high variant), `insider`
(`min_insiders` distinct buyers inside `window_days` with `min_value_usd` of
purchases), and `committee` (the 13 personas vote at every rebalance date over
`history_days`; fundamentals as of `filing_lag_days` earlier; `top_n`,
`min_consensus`, `min_agreement`, `lean`; `notes.periods` on the result lists
every rebalance date's votes and picks). `data_source` picks where daily
prices come from — yfinance is free, FD bills per 90-day chunk — while
earnings, insider trades and fundamentals are always Financial Datasets;
`fd_requests` / `fd_cost_usd` on the result say what a run cost, and persona
snapshots for historical dates are cached in `data/personas.db` so a re-run
of the same dates is free.

Screening rules are individually optional: the UI ticks any subset of the
criteria (market cap, price, growth, margins, ROE/ROIC, leverage, valuation
multiples, FCF yield, payout, volatility, 1w/1m/3m returns, distance from the
52-week high/low) and each rule is `field ≥/≤ value`. A ticker missing a
field fails that rule; `reject_reasons` counts which rules cut the most.
The legacy `market_cap_min/…` fields still work and are folded into rules;
with neither given the four defaults apply. Under yfinance some ratio
fields (e.g. ROIC, payout) may be empty for part of the universe.

```
POST /api/lab/screening              {universe, tickers?, data_source: yfinance|fd, with_earnings?, rules: [{field, op: gte|lte, value}]}
GET  /api/lab/screening/criteria     the 22 rule fields (label, unit, source) + the default rule set
POST /api/lab/backtest               {universe, tickers?, strategy: pead|momentum|insider|committee, data_source: yfinance|fd, holding_days, capital, per_trade, …}
GET  /api/lab/backtest/jobs/{id}     poll a background backtest (the committee strategy always runs as one)
POST /api/lab/event-study            {universe, tickers?, data_source: yfinance|fd, earnings_limit, n_bootstrap, require_eps_surprise, dedupe, group_by: surprise|source}
GET  /api/lab/signals                production anomaly thresholds, read-only
GET  /api/lab/runs[?kind=&limit=]    persisted run log for every tool (+ per-kind counts)
GET  /api/lab/runs/{id}              params + full result of one run

POST /api/lab/committee              {source: holdings|watchlist|tickers|screening, tickers?, personas?, as_of?, top_n?, max_weight?}
GET  /api/lab/committee/runs, /runs/{id}, /personas
GET  /api/lab/committee/scoreboard   per-persona hit rate + vote counts (due / scored per horizon)
POST /api/lab/committee/narrate      {run_id, ticker, persona, language} → LLM explanation for one cell, verdict unchanged
POST /api/lab/committee/backfill     run the forward-return backfill now (scheduler ⑯ does it nightly at 02:30 ET)
```

Cost control (financialdatasets.ai bills per request on the credits plan):
the screener defaults to yfinance for market cap / revenue growth / gross
margin (free; FD is a switch), earnings enrichment is opt-in (one FD request
per candidate), the committee's 省流模式 (default on) skips the news and
insider-trade fetches, market cap is read from the metrics row instead of a
separate call, and every run reports `fd_requests` and `fd_cost_usd`.
`GET /api/lab/committee/pricing` exposes the price table used for estimates.

Frontend: `ai-workbench/app/lab.tsx` (the Lab section), `app/lib/api.ts`
(owner-token fetch helper shared with `page.tsx`).

## Env

| Var | Meaning | Default |
|---|---|---|
| `WEB_OWNER_TOKEN` | required header value in prod; empty = auth off (dev) | *(unset)* |
| `WEB_ARCHIVE_DB` | path to v2's `archive.db` | `<repo>/data/archive.db` |
| `WEB_PERSONAS_DB` | committee votes + snapshot cache (Lab · 投资人委员会) | `<repo>/data/personas.db` |
| `WEB_LAB_DB` | persisted run log for every Lab tool | `<repo>/data/lab.db` |
| `FD_PRICES` | JSON overriding financialdatasets.ai per-request prices used for Lab cost estimates, e.g. `{"news":0.04}` | $0.02 per request for every stock endpoint (account Billing page, 2026-09) |
| `WEB_CORS_ORIGINS` | comma-separated allowed origins | `localhost:5173` |

Plus the v2 runtime env (`FINANCIAL_DATASETS_API_KEY`, `DEEPSEEK_API_KEY`,
`APCA_*`, etc.) since responders call the real modules.
