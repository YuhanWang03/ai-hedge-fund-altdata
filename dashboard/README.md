# AI Hedge Fund · Dashboard

Web dashboard for the [ai-hedge-fund-altdata](../) Telegram bot.

- **Left 70%** — live execution trace: every module, LLM prompt, API call,
  and DB write that runs to answer a query.
- **Right 30%** — Telegram-style chat. Free-form natural language; same NL
  intents the production bot supports.

Two access modes:

- **Owner** — auth via `X-Owner-Token` header; unlimited, no cache.
- **Guest** — anonymous; 5 queries/IP/hour, $0.30/day global budget, 8
  whitelisted intents, replay-cache with per-intent TTL.

## Architecture

```
Browser
  ├─ POST /api/query  (X-Owner-Token | anon)
  │     → FastAPI: classify → cache lookup → budget reserve → spawn task
  │     ← {session_id, sse_url, intent, args, cached?, budget_remaining}
  │
  └─ GET /sse/trace/{session_id}
        ← stream of: session_start, intent_classified, api_call,
                     llm_call, db_write, chat_message, session_end
```

Trace events come from `v2/observability/` — a dormant SDK that the
dashboard installs into the production v2 codebase at startup. Production
Telegram bot / scheduler / streamer do not call `install_all()` and are
unaffected.

## Run locally

```bash
# Backend
cd dashboard/backend
poetry install --no-root  # or: pip install fastapi uvicorn[standard] aiosqlite pydantic
DASHBOARD_OWNER_TOKEN=dev-token \
PYTHONPATH=.:.. \
uvicorn app.main:app --reload --port 8001

# Frontend (separate terminal)
cd dashboard/frontend
npm install
npm run dev          # http://127.0.0.1:5173
```

## Tests

```bash
cd dashboard/backend
PYTHONPATH=.:.. pytest -q
# 28 passed
```

The v2/observability SDK has its own test suite:

```bash
cd ../..      # back to repo root
poetry run pytest v2/observability/ -q
# 11 passed
```

## Deployment

See [deploy/README.md](./deploy/README.md) for the systemd + nginx setup.
A fourth service (`hedge-fund-dashboard.service`) joins scheduler / bot /
streamer.

## Wiring real responders

`backend/app/runner/intent_adapter.py` ships with a `_stub_responder` for
every intent so the demo works without API keys. Replace the entries in
`DISPATCH` with callables that invoke the underlying v2 modules; the
observability hooks pick up every internal call automatically.
