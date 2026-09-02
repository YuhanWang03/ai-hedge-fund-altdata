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
- ⬜ **Phase 2** — panel endpoints (portfolio / risk / watchlist / signals feed)
- ⬜ **Phase 3** — interactive money-flow charts + polish + VPS deploy

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

## Env

| Var | Meaning | Default |
|---|---|---|
| `WEB_OWNER_TOKEN` | required header value in prod; empty = auth off (dev) | *(unset)* |
| `WEB_ARCHIVE_DB` | path to v2's `archive.db` | `<repo>/data/archive.db` |
| `WEB_CORS_ORIGINS` | comma-separated allowed origins | `localhost:5173` |

Plus the v2 runtime env (`FINANCIAL_DATASETS_API_KEY`, `DEEPSEEK_API_KEY`,
`APCA_*`, etc.) since responders call the real modules.
