#!/usr/bin/env bash
# READ-ONLY VPS state collector. Redacts secrets. Never mutates anything.
set +e
ROOT=/opt/bot-s1
# Redaction filter: masks private keys (long hex), and values after secret-ish keys.
redact() {
  sed -E \
    -e 's/(0x[0-9a-fA-F]{6})[0-9a-fA-F]{42,}/\1…REDACTED_LONG_HEX/g' \
    -e 's/((PRIVATE_KEY|SECRET|PASSPHRASE|PASSWORD|API_KEY|POLY_SECRET|CLOB_SECRET|SECRET_KEY|TOKEN|COOKIE)[A-Z_]*[=:][[:space:]]*).*/\1***REDACTED***/gI'
}

sec() { echo; echo "================ $1 ================"; }

sec "SERVICE: is-enabled / is-active"
for s in polybot-live polybot-paper polybot-dashboard bot-s1; do
  printf "%-28s enabled=%s active=%s\n" "$s" "$(systemctl is-enabled $s 2>&1)" "$(systemctl is-active $s 2>&1)"
done

for s in polybot-live polybot-paper polybot-dashboard; do
  sec "SYSTEMCTL STATUS: $s"
  systemctl status $s --no-pager -n 3 2>&1 | redact
  sec "SYSTEMCTL SHOW (limits/restart): $s"
  systemctl show $s -p ExecStart -p User -p WorkingDirectory -p Restart -p RestartSec -p MemoryMax -p MemoryHigh -p CPUQuota -p NRestarts -p ActiveEnterTimestamp -p EnvironmentFiles -p ExecMainPID 2>&1 | redact
done

sec "UNIT FILES (/etc/systemd/system)"
for f in /etc/systemd/system/polybot-live.service /etc/systemd/system/polybot-paper.service /etc/systemd/system/polybot-dashboard.service /etc/systemd/system/bot-s1.service; do
  [ -f "$f" ] && { echo "----- $f -----"; cat "$f" | redact; }
done

sec "FULL FILE TREE (excl .venv/.git/__pycache__/node_modules)"
find "$ROOT" -type f \
  -not -path '*/.venv/*' -not -path '*/.git/*' -not -path '*/__pycache__/*' \
  -not -path '*/node_modules/*' -not -path '*/.pytest_cache/*' 2>/dev/null | sort

sec "PYTHON / SDK VERSIONS"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] && "$PY" --version 2>&1
echo "--- py-clob-client-v2 ---"; "$PY" -m pip show py-clob-client-v2 2>&1 | grep -Ei '^(Name|Version|Location)' | redact
echo "--- py-clob-client (old) ---"; "$PY" -m pip show py-clob-client 2>&1 | grep -Ei '^(Name|Version)' | redact
echo "--- key deps ---"; "$PY" -m pip freeze 2>/dev/null | grep -Ei 'clob|web3|eth-|flask|fastapi|uvicorn|gunicorn|websocket|aiohttp|httpx|sqlalchemy|pandas|numpy|pytest|apscheduler|starlette|pydantic' | redact

sec "SDK IMPORT USAGE"
echo "--- py_clob_client_v2 refs ---"; grep -RIl 'py_clob_client_v2' "$ROOT/polybot" "$ROOT/dashboard" 2>/dev/null | head -40
echo "--- OLD py_clob_client refs ---"; grep -RIl 'py_clob_client\b' "$ROOT/polybot" "$ROOT/dashboard" 2>/dev/null | grep -v v2 | head -40

sec "SAFETY FLAGS (env files, non-secret flags only)"
for e in "$ROOT/env.txt" "$ROOT/env.paper.txt" "$ROOT/env.dashboard.txt"; do
  echo "----- $e (KEYS ONLY) -----"
  [ -f "$e" ] && grep -oE '^[A-Z0-9_]+' "$e" | sort -u
  echo "  -- safety flags --"
  [ -f "$e" ] && grep -iE '^(ENABLE_LIVE_TRADING|DRY_RUN|LIVE|PAPER|TRADING_ENABLED|ALLOW_LIVE|SIMULATE|MODE)[A-Z_]*=' "$e"
done

sec "PORTS LISTENING"
ss -tlnp 2>/dev/null | grep -E ':8080|:8000|:5000|python|uvicorn|gunicorn' | redact

sec "PROCESSES (bot-related)"
ps -eo pid,ppid,user,%cpu,%mem,etime,cmd 2>/dev/null | grep -Ei 'polybot|run_paper|run_dashboard|run\.py|uvicorn|gunicorn|python.*bot' | grep -v grep | redact

sec "DATA DIRS + SQLITE DBS"
find "$ROOT/data" -type f 2>/dev/null | sort
echo "--- db sizes ---"; find "$ROOT/data" -name '*.db' -o -name '*.sqlite*' 2>/dev/null | xargs -r ls -la 2>/dev/null
for db in $(find "$ROOT/data" -name '*.db' 2>/dev/null); do
  echo "--- SCHEMA: $db ---"
  sqlite3 "$db" ".tables" 2>&1 | head -20
  sqlite3 "$db" "PRAGMA journal_mode;" 2>&1
done

sec "STATE DIR"
find "$ROOT/state" -type f 2>/dev/null | xargs -r ls -la 2>/dev/null

sec "LOGS (list + sizes)"
find "$ROOT/logs" "$ROOT/data" -name '*.log' 2>/dev/null | xargs -r ls -la 2>/dev/null | redact
sec "LOG TAILS (redacted, last 15 each)"
for lg in $(find "$ROOT/logs" -name '*.log' 2>/dev/null | head -8); do
  echo "----- tail $lg -----"; tail -n 15 "$lg" 2>&1 | redact
done

sec "SECRET LEAK SCAN (logs/html/json — count only, no values)"
grep -RIlE '(PRIVATE_KEY|-----BEGIN|0x[0-9a-fA-F]{60,})' "$ROOT/logs" "$ROOT/data" "$ROOT/dashboard" 2>/dev/null | head -20 || echo "no obvious leaks in scanned dirs"

sec "GIT STATE"
if [ -d "$ROOT/.git" ]; then git -C "$ROOT" log --oneline -5 2>&1; echo "--- status ---"; git -C "$ROOT" status -s 2>&1 | head; else echo "NOT a git repo"; fi

sec "DONE"
