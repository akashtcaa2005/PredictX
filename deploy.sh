#!/usr/bin/env bash
# Deploy + start the Polymarket bot in DRY-RUN inside the Camber workspace.
#
# - Gates on CAMBER_API_TOKEN (via preflight); refuses to continue without it.
# - Reads all remaining config from env.txt.
# - Installs dependencies, loads the compiled strategy read-only, verifies
#   Polymarket connectivity, then starts DRY-RUN.
# - Never prints or logs secrets. Never enables LIVE mode automatically.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p logs state
# Tee a detailed, timestamped deploy log (no secrets are ever emitted here).
exec > >(tee -a logs/deploy.log) 2>&1
ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) [deploy] $*"; }

log "workspace: $(pwd)"

# 1) toolchain + dependencies -------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  log "creating virtualenv (.venv)"
  python3.13 -m venv --system-site-packages .venv
fi
log "installing dependencies from requirements.txt (idempotent)"
.venv/bin/pip install --disable-pip-version-check -q -r requirements.txt 2>&1 | sed 's/^/  pip: /' || true

# 2) preflight: token gate + strategy load + connectivity ---------------------
log "running preflight checks"
set +e
.venv/bin/python scripts/preflight.py
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
  log "PREFLIGHT FAILED (rc=$rc)"
  case "$rc" in
    3) log "cause: CAMBER_API_TOKEN missing — set it in the environment or env.txt";;
    4) log "cause: compiled strategy failed to load";;
    5) log "cause: Polymarket API unreachable from this workspace (egress blocked)";
       log "attempting a bounded DRY-RUN start anyway to record the bot's own handling…"
       export ENABLE_LIVE_TRADING=false
       timeout -k 5 30 .venv/bin/python run.py || true
       log "dry-run start attempt finished — see logs/execution.log";;
    6) log "cause: configuration invalid in env.txt";;
  esac
  log "not entering service; resolve the above and re-run ./deploy.sh"
  exit "$rc"
fi

# 3) start DRY-RUN in the background (never LIVE automatically) ----------------
log "preflight passed — starting bot in DRY-RUN (background)"
export ENABLE_LIVE_TRADING=false
nohup .venv/bin/python run.py >> logs/stdout.log 2>&1 &
echo $! > state/bot.pid
log "bot started in DRY-RUN, PID $(cat state/bot.pid)"
log "follow logs:  tail -f logs/execution.log  (also orders.log / strategy.log / trade.log)"
log "stop:         kill \$(cat state/bot.pid)"
log "deploy complete (DRY-RUN). LIVE mode is NOT enabled."
