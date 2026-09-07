#!/usr/bin/env bash
# Redeploy the web backend + ai-workbench frontend (+ scheduler) on the VPS.
#
#   ssh root@<vps>
#   cd /root/hedge-fund && bash web/deploy/redeploy.sh [git-ref]
#
# git-ref defaults to origin/main. The script fast-forwards the checked-out
# branch to it, restarts the FastAPI backend, rebuilds the vinext workbench
# only when ai-workbench/ changed, and restarts the scheduler only when
# v2/scheduler/ changed. Exits on the first error.
set -euo pipefail

REPO=/root/hedge-fund
REF=${1:-origin/main}
cd "$REPO"

echo "== git: fetch + fast-forward to $REF"
git fetch origin --prune
BEFORE=$(git rev-parse HEAD)
git merge --ff-only "$REF"
AFTER=$(git rev-parse HEAD)
echo "   $BEFORE -> $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then echo "   nothing new"; fi
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER" || true)

echo "== backend: restart hedge-fund-web"
sudo systemctl restart hedge-fund-web
sleep 2
curl -sf http://127.0.0.1:8100/api/health >/dev/null && echo "   /api/health ok" || { echo "   backend not healthy — tail logs/web.err"; tail -n 30 logs/web.err; exit 1; }

if echo "$CHANGED" | grep -q '^ai-workbench/' || [ ! -d ai-workbench/.next ] && [ ! -d ai-workbench/dist ]; then
  echo "== frontend: rebuild ai-workbench"
  (cd ai-workbench && npm ci --no-audit --no-fund && npm run build)
  sudo systemctl restart hedge-fund-workbench
  sleep 3
  curl -sf -o /dev/null http://127.0.0.1:3000/ && echo "   workbench :3000 ok" || { echo "   workbench not serving — tail logs/workbench.err"; tail -n 30 logs/workbench.err; exit 1; }
else
  echo "== frontend: ai-workbench unchanged, skipping build"
fi

if echo "$CHANGED" | grep -q '^v2/scheduler/'; then
  echo "== scheduler: restart hedge-fund-scheduler (new/changed jobs)"
  sudo systemctl restart hedge-fund-scheduler
else
  echo "== scheduler: unchanged, not restarted"
fi

echo "== done. services:"
systemctl --no-pager --plain list-units 'hedge-fund-*' | sed -n '1,8p'
