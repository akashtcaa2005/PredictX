# RUNNING-SYSTEM EVIDENCE (collected read-only from VPS polymarket-bot / 172.233.54.65)
Collected 2026-08-08 ~03:35–03:42 UTC. Source tree mirrored (no secrets) at ../vps-src.
Full raw dump: ../vps-state/state.txt

## Topology
- Operator is on a LOCAL Kali laptop; the sandbox has NO direct internet to Polymarket (curl times out).
- Real system runs on VPS `polymarket-bot` (root@172.233.54.65), Ubuntu 6.8, 2 vCPU / 3.8Gi RAM / 79G disk (5% used). Project at /opt/bot-s1 owned by non-root `botuser`.
- ALL live/paper API verification must happen ON the VPS (it is in NL, not geoblocked; WS connected, ~25ms latency).

## Services (systemd)
- polybot-live.service      : enabled, ACTIVE running (pid 38928), run.py, dry-run.
- polybot-dashboard.service : enabled, ACTIVE running (pid 39309), run_dashboard.py, 127.0.0.1:8080.
- polybot-paper.service     : enabled, **STOPPED (inactive/dead)**. Manually stopped 03:39:42 via
  `sudo systemctl stop polybot-paper.service --no-block` from a root SSH session (520). NOT automated
  (no cron, no timers, dashboard audit empty). Restart=always does NOT restart a clean `systemctl stop`,
  so it stays down until manually started. NRestarts=0 for all three (no crash-looping).
- bot-s1.service           : LEGACY unit, disabled + inactive. Whole-tree ReadWritePaths=/opt/bot-s1
  (less locked-down than polybot-live). Spec calls the live engine "bot-s1.service" but the REAL live
  engine is polybot-live.service. bot-s1.service is a stale duplicate.

## SAFETY (verified at runtime)
- ENABLE_LIVE_TRADING=false hardcoded in polybot-live.service AND bot-s1.service unit files, with comment:
  process env wins over env.txt (settings.load_env_file override=False) => env.txt CANNOT arm live trading.
- /proc/<live pid>/environ contains ENABLE_LIVE_TRADING=false and NO credential keys (secrets are read
  from env.txt at runtime, never placed in the systemd environment => not exposed via environ).
- Live engine heartbeat: wallet 0x2918…E1D2, balance 0.0, orders 0, positions 0, pnl 0, risk.halted=false,
  daily_loss_limit=50, WS market+user connected (reconnects=0), latency avg 25ms p95 35ms. REAL data.
- Auth gate (unauthenticated HTTP): /api/snapshot, /api/users, /api/control/service (paper & live),
  /api/control/emergency-stop ALL return 401. GET / =200 (login shell), /healthz=200. Served HTML leaks 0 secrets.
- Live readiness.json: live_ready=false. checks: authentication ok, not_closed_only ok, maker_wallet ok;
  usdc_allowance ok=false (0.0 on all 3 spenders), deposit_wallet_enabled ok=false, usdc_balance 0.0<1.0.
  missing_requirements clearly list EXTERNAL blockers (Enable Trading flow + deposit USDC). Honest, not bypassed.

## CLOB V2
- py_clob_client_v2==1.1.0 installed; OLD py-clob-client NOT installed.
- v2 imports in: exchange/auth.py, exchange/clob.py, dashboard/server.py, wallet/{allowance,maker,approval}.py, paper/main.py.
- paper/main.py line 51: `for module_name in ("py_clob_client_v2.client","py_clob_client.client")` = v2-first fallback tuple; benign (only v2 installed). No real dependency on old SDK anywhere.
- Python 3.13.15. Deps: fastapi 0.141, starlette 1.4, uvicorn 0.52, httpx 0.28, web3 7.16, websockets 15, pydantic 2.13, pytest 9.1.

## Databases (all WAL, opened mode=ro by dashboard)
- data/dashboard/dashboard.db: users(1), candles(1272), notifications(8), sessions(0), login_attempts(0),
  journal(0), audit(0), app_settings(0). indexes on ts/user.
- data/live/engine.db & data/paper/engine.db: orders(0), fills(0), trades(0), positions(2), equity(54/59),
  events(1/4), engine_state, strategy_state(1). indexes: events_ts, fills_ts, orders_created, orders_market,
  orders_status, trades_exit. ZERO completed trades => analytics should read N/A (Phase 10 test).
- separate trees: data/live, data/paper, data/dashboard each with own logs/ + state/.

## Tests / preflight (on VPS)
- pytest -q => 71 passed, 1 warning (starlette httpx deprecation). Includes test_everything_requires_authentication,
  test_paper_offline, test_engine_offline, test_dashboard_offline, test_allowance_offline, test_startup_offline,
  test_strategy_wiring, test_telemetry_offline.
- scripts/preflight.py => PREFLIGHT PASSED. mode=DRY-RUN, CAMBER token present, strategy CheapBuyStrategy(1c_buy)
  loaded, clob+gamma reachable, signer 0x2918…, maker/deposit 0x0f88…427a, signature_type=1 POLY_PROXY,
  L2 creds belong to signer, closed_only=false. (Non-fatal: /auth/api-key 400 "Could not create api key" —
  account already has creds / not fully enabled.)

## KNOWN RUNTIME DEFECT — dashboard live_sync EAGAIN
- polybot-dashboard logs repeated ERROR: `py_clob_client_v2.http_helpers.helpers | request error: [Errno 11]
  Resource temporarily unavailable` — ~42 per 1000 log lines, in BATCHES of 2-4 at identical timestamps,
  each sync cycle (~every 70-80s incl. slow interval).
- live_sync.py authenticates as the live account (Settings.load(mode="live") -> build_clob_client reads env.txt
  private key) and wraps in ReadOnlyExchange (method allowlist: health, get_order_book, get_orders, get_trades,
  get_collateral_balance, get_collateral_allowances, ...). It then calls _sync_health/_sync_balance/_sync_orders/
  _sync_trades. The live ENGINE makes the same calls with NO such error.
- HYPOTHESIS (to confirm in Lane D): multiple authenticated REST calls run CONCURRENTLY (asyncio.gather +
  asyncio.to_thread) over a SHARED non-thread-safe py_clob_client_v2 `requests` session/connection pool =>
  socket EAGAIN. Batches-at-same-timestamp fingerprint fits concurrent collision. Fix: serialize the
  authenticated REST calls (lock or sequential await), or per-call session. Engine avoids it by calling sequentially.
- Impact: dashboard live account panel (balance/allowances/orders/trades) likely shows stale/UNKNOWN.
  Note: the dashboard also reads data/live/engine.db (engine's own synced source-of-truth) which is fine.

## LEGACY / CLEANUP observations
- Top-level /opt/bot-s1/state/ (dashboard.db 20KB old, last_state.json, readiness.json, status.json) and
  /opt/bot-s1/logs/ (execution.log 3.5MB, wallet.log 182KB, websocket.log 164KB, orders.log with old
  0xTESTMARKET dry-run sims) are LEGACY from the pre-isolation single-service design. Active engines now write
  under data/{live,paper}/{logs,state}. These stale trees are messy but not harmful.
- logs/audit/orders.log shows historical `dry_run=False` order-submission ATTEMPTS (2026-08-07 09:19/18:15/18:17)
  with error RetryError('post_order failed after 1 attempts') — from scripts/live_execution_audit.py, failed
  safely (no allowance/balance). DO NOT run that script (it attempts real orders).
- Two dashboard code trees: /opt/bot-s1/dashboard/ (ACTIVE, run_dashboard.py) and legacy
  /opt/bot-s1/polybot/dashboard/server.py (verify unused).
- sqlite3 CLI not installed on VPS (used venv python for introspection).

## OPEN QUESTIONS for source analysis (workflow)
- Does dashboard fabricate any value, or correctly show N/A / UNKNOWN / NO ACTIVE MARKET? (Phases 5-11)
- Sharpe/Sortino sample-size gating; candle-source labeling (Phases 8,10,11).
- Log rotation / DB retention configured anywhere? (Phases 19,20) — disk at 5% now but must not silently fill.
- Strategy panel: does it invent confidence/SL/TP/accuracy the strategy doesn't provide? (Phase 9)
- Orders table fields, trade journal auto-recording, alerts optionality (Phases 12,13,15).
- CSRF, session timeout, RBAC, password hashing, audit logging details (Phase 18).
