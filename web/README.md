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

## Lab · 投资人委员会

`routers/committee.py` exposes the thirteen rule-based investor personas
(`v2/personas/`) to the workbench's Lab section:

```
POST /api/lab/committee            {source: holdings|watchlist|tickers|screening, tickers?, personas?, as_of?, top_n?, max_weight?}
GET  /api/lab/committee/runs       persisted run log (data/personas.db, override with WEB_PERSONAS_DB)
GET  /api/lab/committee/runs/{id}  full result of one run
GET  /api/lab/committee/personas   persona metadata for the picker
GET  /api/lab/committee/scoreboard per-persona hit rate once forward returns are back-filled
POST /api/lab/committee/narrate    {run_id, ticker, persona, language} → LLM explanation for one cell, verdict unchanged
POST /api/lab/committee/backfill   run the forward-return backfill now (scheduler ⑯ does it nightly at 02:30 ET)
```

Fundamentals snapshots are cached per (ticker, day), so re-running the same
day costs no API calls. No LLM is involved in the verdicts.

## Env

| Var | Meaning | Default |
|---|---|---|
| `WEB_OWNER_TOKEN` | required header value in prod; empty = auth off (dev) | *(unset)* |
| `WEB_ARCHIVE_DB` | path to v2's `archive.db` | `<repo>/data/archive.db` |
| `WEB_PERSONAS_DB` | committee run log + snapshot cache (Lab · 投资人委员会) | `<repo>/data/personas.db` |
| `WEB_CORS_ORIGINS` | comma-separated allowed origins | `localhost:5173` |

Plus the v2 runtime env (`FINANCIAL_DATASETS_API_KEY`, `DEEPSEEK_API_KEY`,
`APCA_*`, etc.) since responders call the real modules.
