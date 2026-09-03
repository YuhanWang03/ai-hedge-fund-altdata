# Deploying the web (Ubuntu + nginx + systemd)

Adds a **fifth** systemd service alongside `hedge-fund-scheduler`,
`hedge-fund-bot`, `hedge-fund-streamer` (and the frozen `hedge-fund-dashboard`).
The backend runs on 127.0.0.1:8100; nginx serves the React bundle and proxies
`/api` to it. Running on the VPS fixes the local-network issue (Tavily / OpenAI
/ yfinance unreachable from the dev machine).

Run everything **on the VPS** (`ssh root@138.197.22.39`).

## 1. Get the code

```bash
cd /root/hedge-fund
git fetch origin
git checkout feat/web-rebuild      # or main once the PR is merged
git pull
```

## 2. Backend deps + owner token

```bash
# fastapi/uvicorn into the project's poetry env (once).
# Use poetry's full path — a non-login SSH shell doesn't have ~/.local/bin on PATH.
/root/.local/bin/poetry run pip install fastapi "uvicorn[standard]"

mkdir -p /root/hedge-fund/logs
sudo mkdir -p /etc/hedge-fund
echo "WEB_OWNER_TOKEN=$(openssl rand -hex 24)" | sudo tee /etc/hedge-fund/web.env
sudo chmod 600 /etc/hedge-fund/web.env
# note the token — you'll paste it into the web UI's top-right box.
sudo cat /etc/hedge-fund/web.env
```

## 3. Backend service

```bash
sudo cp /root/hedge-fund/web/deploy/hedge-fund-web.service \
        /etc/systemd/system/hedge-fund-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now hedge-fund-web
sudo systemctl status hedge-fund-web --no-pager
curl -s http://127.0.0.1:8100/api/health    # {"status":"ok",...}
```

## 4. Frontend build

```bash
cd /root/hedge-fund/web/frontend
npm install
npm run build                                # → dist/
sudo rm -rf /var/www/hedge-fund-web
sudo mkdir -p /var/www/hedge-fund-web
sudo cp -r dist/* /var/www/hedge-fund-web/
```

## 5. nginx

```bash
sudo cp /root/hedge-fund/web/deploy/nginx-web.conf \
        /etc/nginx/sites-available/hedge-fund-web.conf
sudo ln -sf /etc/nginx/sites-available/hedge-fund-web.conf /etc/nginx/sites-enabled/
# disable the frozen old dashboard site if present:
sudo rm -f /etc/nginx/sites-enabled/dashboard.conf
# also remove the stock default site if it grabs port 80:
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 6. Open it

Browse to `http://138.197.22.39/`, paste the `WEB_OWNER_TOKEN` from step 2 into
the top-right box, click 保存. Dashboard on the left, chat on the right — and
`NVDA 为什么涨` now works (clean VPS network).

## Redeploy after code changes

```bash
cd /root/hedge-fund && git pull
# backend:
sudo systemctl restart hedge-fund-web
# frontend (only if web/frontend changed):
cd web/frontend && npm run build \
  && sudo rm -rf /var/www/hedge-fund-web && sudo mkdir -p /var/www/hedge-fund-web \
  && sudo cp -r dist/* /var/www/hedge-fund-web/
```

## Notes

- **Auth**: single owner token over plain HTTP. Fine for personal use; for a
  real domain add nginx + Let's Encrypt (HTTPS) so the token isn't sent in the
  clear.
- **Ports**: backend 127.0.0.1:8100 (not exposed); only nginx :80 is public.
- **Memory**: uvicorn ~60 MB; comfortably fits alongside the other services.
