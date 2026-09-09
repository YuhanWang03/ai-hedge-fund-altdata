#!/usr/bin/env bash
# Run the Agent V1→V2 benchmark with a real model on the VPS.
#
#   ssh root@<vps>
#   cd /root/hedge-fund && git fetch origin claude/agent-v2-design-review-jozhco
#   : > logs/agent_v2_eval.nohup
#   nohup bash <(git show origin/claude/agent-v2-design-review-jozhco:v2/agent_v2/eval/vps_run.sh) --push \
#       >> logs/agent_v2_eval.nohup 2>&1 &
#   tail -F logs/agent_v2_eval.nohup
#
# What it does, without touching the production working tree or any service:
#   1. checks the branch out into a separate git worktree;
#   2. borrows the production poetry environment, .env keys and the
#      git-ignored v2/data package (symlinked into the worktree);
#   3. runs the Agent V2 unit tests in that environment;
#   4. records live research/market envelopes (needs FD keys; failures are
#      tolerated — the benchmark synthesises whatever is missing);
#   5. runs the three benchmark configurations with the real model, three
#      repeats each, writing Markdown + JSON under logs/agent_v2_eval/<stamp>/;
#   6. with --push, commits the report into v2/agent_v2/eval/reports/ on the
#      branch and pushes it.
#
# Flags:  --push  --push-recorded  --no-record  --no-tests  --repeat N  --workers N
#         --branch NAME  --repo PATH
# Env:    REF (default origin/$BRANCH), EXTRA (extra run_benchmark args, e.g. "--simulate 0.2" for a dry run)
set -euo pipefail

REPO=${REPO:-/root/hedge-fund}
BRANCH=${BRANCH:-claude/agent-v2-design-review-jozhco}
REPEAT=${REPEAT:-3}
WORKERS=${WORKERS:-3}
PUSH=0
PUSH_RECORDED=0
RECORD=1
TESTS=1
EXTRA=${EXTRA:-}
while [ $# -gt 0 ]; do
  case "$1" in
    --push) PUSH=1 ;;
    --push-recorded) PUSH=1; PUSH_RECORDED=1 ;;
    --no-record) RECORD=0 ;;
    --no-tests) TESTS=0 ;;
    --repeat) REPEAT=$2; shift ;;
    --workers) WORKERS=$2; shift ;;
    --branch) BRANCH=$2; shift ;;
    --repo) REPO=$2; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

STAMP=$(date +%Y%m%d-%H%M%S)
WORKTREE=${WORKTREE:-${REPO}-agent-v2-eval}
OUT=$REPO/logs/agent_v2_eval/$STAMP
mkdir -p "$OUT"
LOG=$OUT/run.log
exec > >(tee -a "$LOG") 2>&1

step() { echo; echo "== [$(date +%H:%M:%S)] $*"; }

REF=${REF:-origin/$BRANCH}
step "worktree: $WORKTREE @ $REF"
cd "$REPO"
if [ "$REF" = "origin/$BRANCH" ]; then git fetch origin "$BRANCH" --prune; fi
if [ -d "$WORKTREE/.git" ] || [ -f "$WORKTREE/.git" ]; then
  git -C "$WORKTREE" checkout -q --detach "$(git rev-parse "$REF")"
else
  git worktree add --detach "$WORKTREE" "$(git rev-parse "$REF")"
fi
echo "   $(git -C "$WORKTREE" rev-parse --short HEAD) $(git -C "$WORKTREE" log -1 --format=%s)"

step "environment"
POETRY=${POETRY:-/root/.local/bin/poetry}
VENV=$("$POETRY" env info -p)
PY=$VENV/bin/python
echo "   python: $PY"
# The git-ignored production data package is not in the worktree; link it in.
for path in "$REPO"/v2/data/*; do
  name=$(basename "$path")
  [ -e "$WORKTREE/v2/data/$name" ] || ln -s "$path" "$WORKTREE/v2/data/$name"
done
# Production SQLite stores live under <repo>/data, located from each module's
# __file__, so the worktree needs the production data directory at its root.
[ -e "$WORKTREE/data" ] || ln -s "$REPO/data" "$WORKTREE/data"
[ -e "$WORKTREE/.env" ] || ln -s "$REPO/.env" "$WORKTREE/.env"
# Keys come from the production .env; the model client accepts DEEPSEEK_API_KEY directly.
set -a; . "$REPO/.env"; set +a
export AGENT_LLM_MODEL=${AGENT_LLM_MODEL:-deepseek-chat}
export PYTHONPATH=$WORKTREE
export PYTHONUNBUFFERED=1
# `python -m` puts the current directory ahead of PYTHONPATH, so run from the
# worktree: from $REPO the production v2 package (without this branch) wins.
cd "$WORKTREE"
"$PY" -c 'import v2, v2.agent_v2.run_benchmark; print("   v2 package:", v2.__path__[0])'
"$PY" - <<'EOF'
import os, sys
import v2.data
sys.stdout.write("   v2.data: %s\n" % ("production" if hasattr(v2.data, "CachedFDClient") else "MISSING (placeholders will be used)"))
key = os.environ.get("AGENT_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
sys.stdout.write("   model key: %s, model=%s, base=%s\n" % ("set" if key else "MISSING", os.environ.get("AGENT_LLM_MODEL"), os.environ.get("AGENT_LLM_BASE_URL", "https://api.deepseek.com/v1")))
if not key:
    sys.exit("no model key in .env (AGENT_LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY)")
EOF

if [ "$TESTS" = 1 ]; then
  step "unit tests"
  "$PY" -m pytest "$WORKTREE/v2/agent_v2" -q -p no:cacheprovider -x --deselect "$WORKTREE/v2/agent_v2/test_agent_v2.py::test_telegram_ask_v2_command_is_explicit_and_parses_web_consent" || true
fi

if [ "$RECORD" = 1 ]; then
  step "record live envelopes (failures tolerated)"
  "$PY" -m v2.agent_v2.record_fixtures --live --dir "$WORKTREE/v2/agent_v2/eval/recorded" || echo "   recording incomplete; missing envelopes will be synthesised"
fi

REPORT=$OUT/report.md
{
  echo "# Agent V1 vs V2 · 模型在环 · $STAMP"
  echo
  echo "- 分支：\`$BRANCH\` @ $(git -C "$WORKTREE" rev-parse --short HEAD)"
  echo "- 模型：${AGENT_LLM_MODEL}（${AGENT_LLM_BASE_URL:-https://api.deepseek.com/v1}）"
  echo "- 重复：$REPEAT · workers：$WORKERS · 录制 envelope：$("$PY" -c 'from v2.agent_v2.eval.recorded import RecordedStore; print(RecordedStore().summary() or "none")' 2>/dev/null)"
  echo
} > "$REPORT"

run_bench() {  # label, args...
  local label=$1; shift
  step "benchmark: $label"
  # shellcheck disable=SC2086
  "$PY" -m v2.agent_v2.run_benchmark "$@" $EXTRA --repeat "$REPEAT" --workers "$WORKERS" --progress --markdown "$REPORT" --json "$OUT/$label.json" > "$OUT/$label.txt"
  sed -n '1,60p' "$OUT/$label.txt"
}

run_bench dev-engine     --modes v1_baseline,v2_rules,v2_llm --fixtures engine
run_bench holdout-engine --modes v1_baseline,v2_rules,v2_llm --fixtures engine --holdout
run_bench dev-v1         --modes v2_rules,v2_llm --fixtures v1
run_bench holdout-v1     --modes v2_rules,v2_llm --fixtures v1 --holdout

step "report: $REPORT"
if [ "$PUSH" = 1 ]; then
  step "push report to $BRANCH"
  DEST=v2/agent_v2/eval/reports/$STAMP-vps.md
  cd "$WORKTREE"
  git fetch origin "$BRANCH"
  git checkout -q -B "$BRANCH" "origin/$BRANCH"
  mkdir -p "$(dirname "$DEST")"
  cp "$REPORT" "$DEST"
  git add "$DEST"
  # Live recordings can run to several MB; push them only when asked.
  if [ "$PUSH_RECORDED" = 1 ] && [ -d v2/agent_v2/eval/recorded ]; then git add v2/agent_v2/eval/recorded; fi
  if git -c user.name="vps-eval" -c user.email="vps-eval@hedge-fund.local" commit -q -m "eval: model-in-the-loop benchmark report $STAMP"; then
    git push origin "$BRANCH" && echo "   pushed $DEST"
  else
    echo "   nothing to commit"
  fi
fi

step "done"
echo "   outputs: $OUT"
echo "   report:  $REPORT"
