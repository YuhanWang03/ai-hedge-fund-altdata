# Deploying the dashboard (Ubuntu 24.04 + nginx + systemd)

This adds a **fourth** systemd service alongside `hedge-fund-scheduler`,
`hedge-fund-bot`, `hedge-fund-streamer`.

## 0. Layout on the VPS

The dashboard lives next to `v2/` inside the existing repo:

```
/root/hedge-fund/
├── v2/                      # the production agent codebase
├── scripts/                 # production entrypoints
└── dashboard/
    ├── backend/             # FastAPI app, run by hedge-fund-dashboard.service
    ├── frontend/            # vite source — built and served by nginx
    ├── data/                # SQLite db (created on first run)
    └── deploy/              # this directory
```

## 1. Backend

```bash
cd /root/hedge-fund/dashboard/backend
poetry install --no-root
mkdir -p /root/hedge-fund/dashboard/data /root/hedge-fund/logs
```

Generate an owner token and put it in `/etc/hedge-fund/dashboard.env`
(referenced by the systemd unit):

```bash
sudo mkdir -p /etc/hedge-fund
echo "DASHBOARD_OWNER_TOKEN=$(openssl rand -hex 32)" | sudo tee /etc/hedge-fund/dashboard.env
sudo chmod 600 /etc/hedge-fund/dashboard.env
```

Install the systemd unit:

```bash
sudo cp /root/hedge-fund/dashboard/deploy/hedge-fund-dashboard.service \
        /etc/systemd/system/hedge-fund-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now hedge-fund-dashboard
sudo systemctl status hedge-fund-dashboard
```

Quick smoke:

```bash
curl http://127.0.0.1:8001/api/health
# {"ok": true, "observability_hooks": ["fd:12", "deepseek:1", "tavily:1", "intent:1", "archive:N"]}
```

## 2. Frontend

Build static bundle locally OR on the VPS:

```bash
cd /root/hedge-fund/dashboard/frontend
npm install
npm run build
sudo mkdir -p /var/www/dashboard
sudo cp -r dist/* /var/www/dashboard/
```

## 3. nginx

Use `nginx.conf.snippet` in this directory as a starting point. The two
critical pieces:

- `/api/` → standard reverse proxy
- `/sse/` → `proxy_buffering off`, long read timeout, `X-Accel-Buffering: no`

Reload:

```bash
sudo cp nginx.conf.snippet /etc/nginx/sites-available/dashboard.conf
sudo ln -sf /etc/nginx/sites-available/dashboard.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 4. Owner login

Visit the dashboard in a browser. The default UI is a `GUEST` session
(rate-limited, budget-capped). Click `owner login` in the top-right and
paste the token from `/etc/hedge-fund/dashboard.env`. The token is stored
in `localStorage`; it's sent on every request as `X-Owner-Token`.

## 5. Wiring real responders

`dashboard/backend/app/runner/intent_adapter.py` ships with `_stub_responder`
for every intent so the dashboard demo works without API keys. To use the
production v2 pipeline for real answers, replace each `DISPATCH` entry
with a callable that takes a `dict` of arguments and returns the bot's
reply text. The observability hooks installed at startup will record FD,
DeepSeek, Tavily, intent, and archive calls automatically — no further
code changes needed.

## 6. Memory + cost

- Backend (uvicorn + sqlite): ~60 MB resident.
- Combined with the three existing services, ~395 MB — still fits the 1 GB
  DO droplet with comfortable headroom.
- Each guest query at the worst case (chain or explain_move with fresh
  Tavily + 2× DeepSeek) costs roughly $0.012. The $0.30 daily cap therefore
  buys ~25 fresh runs/day; cache hits cost $0.
