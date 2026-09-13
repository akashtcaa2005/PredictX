# Polybot 24/7 Trading Dashboard

A production monitoring and control plane for the Polymarket bot. Everything runs
on the VPS as three independent systemd services; your laptop, phone or tablet is
only a browser.

```
                    ┌──────────────────────────────────────────────┐
   browser ── https─┤ nginx (TLS)  →  polybot-dashboard.service     │
                    │                   ├── reads  data/paper/engine.db  (ro)
                    │                   ├── reads  data/live/engine.db   (ro)
                    │                   ├── reads  Polymarket account    (ro)
                    │                   └── drives systemd (3 units, 3 verbs)
                    ├──────────────────────────────────────────────┤
                    │ polybot-paper.service  → data/paper/          │
                    │ polybot-live.service   → data/live/           │
                    └──────────────────────────────────────────────┘
```

Close the browser and nothing stops. The engines keep trading; the dashboard keeps
recording. Reopen it and you get the full history back.

---

## 1. What each service does

| Service | Process | Wallet access | Writes to |
|---|---|---|---|
| `polybot-paper` | `run_paper.py` | **none** — credentials are deleted from its environment at startup | `data/paper/` |
| `polybot-live` | `run.py` | full (from `env.txt`) | `data/live/` |
| `polybot-dashboard` | `run_dashboard.py` | **read-only** | `data/dashboard/` |

Each restarts automatically on crash (`Restart=always`), has a memory ceiling and
a CPU quota sized for a 2 vCPU / 4 GB box, and is sandboxed with systemd's
`ProtectSystem=strict` plus an explicit `ReadWritePaths` list.

### Paper and live are genuinely separate

Not a flag — separate processes, separate databases, separate logs, separate
config files, separate risk engines, separate strategy state, separate kill
switches. The paper engine additionally:

* calls `scrub_wallet_secrets()` before anything else, deleting
  `POLYMARKET_PRIVATE_KEY`, `POLY_API_*` and `POLY_BUILDER_*` from its own
  environment and forcing `ENABLE_LIVE_TRADING=false`;
* has `UnsetEnvironment=` for the same variables in its unit file;
* builds a CLOB client with **no key and no credentials**, which is physically
  incapable of signing an order;
* can only write `data/paper/` (enforced by systemd).

It is not a simplified simulation. It runs the same compiled strategy, the same
`Broker`, the same `OrderManager`, the same `RiskEngine` and the same
`PortfolioManager` as live trading — only the venue and the wallet are swapped.
Orders match against **real order-book depth** streamed from the public CLOB
websocket, with configurable latency, partial fills, slippage and fees.

---

## 2. Install

```bash
git clone <your repo> /opt/polybot && cd /opt/polybot
sudo ./deploy/install.sh
```

The installer creates the `polybot` system user, builds the virtualenv, installs
the three units, installs a narrowly scoped sudoers rule, generates a session
secret, and starts the dashboard. It is idempotent — re-run it to upgrade.

Then:

```bash
# 1. the initial admin password is printed once, here:
journalctl -u polybot-dashboard | grep 'INITIAL ADMIN'

# 2. configure your markets
sudo -u polybot nano /opt/polybot/env.paper.txt     # MARKETS=0x...
sudo -u polybot nano /opt/polybot/env.txt           # MARKETS=0x...

# 3. paper first
sudo systemctl start polybot-paper

# 4. TLS
sudo nano /etc/nginx/sites-available/polybot        # server_name
sudo certbot --nginx -d dashboard.example.com
sudo nginx -t && sudo systemctl reload nginx
```

Live trading stays off until you set `ENABLE_LIVE_TRADING=true` in `env.txt` and
start `polybot-live` yourself. `systemctl start polybot-live` with the flag unset
runs a dry-run, not a funded session.

### Configuration files

| File | Owns |
|---|---|
| `env.txt` | live wallet key, live markets, live risk limits |
| `env.paper.txt` | paper markets, virtual balance, simulation realism, paper risk limits |
| `env.dashboard.txt` | bind address, TLS, session secret, alert thresholds, notification channels |

All three are `chmod 600` and git-ignored. Templates are the matching
`*.example.txt` files, which document every option.

---

## 3. What the dashboard shows

### Live account (read straight from Polymarket)

The dashboard authenticates with the same credentials as the live engine and
polls the exchange itself, so wallet and account state are correct **whether or
not the bot is running**. An authenticated `user` websocket acts as a trigger:
when an order or fill event arrives the next REST poll happens immediately
instead of waiting for its interval. REST stays the source of truth, so a dropped
socket degrades to polling rather than to stale numbers.

* USDC balance, available, locked (collateral committed to resting buys), buying
  power (`min(available, allowance)`)
* deposit wallet, signer wallet, signature type, credential source, builder status
* per-spender USDC allowances and whether they clear your configured minimum
* open orders, fills, trade history, positions, portfolio value, unrealised and
  realised PnL, fees paid, exposure
* CLOB reachability, websocket state, sync latency, error count, last sync time

**The dashboard cannot trade.** Its exchange client is wrapped in
`ReadOnlyExchange`, an allowlist proxy — `place_order`, `cancel`, `create_order`
and `post_order` raise `AttributeError` rather than resolving.

### Markets

BTC / ETH / SOL / XRP over 5-minute and 15-minute windows, discovered from Gamma
every 60 s. Duration is inferred from each market's **actual start and end
timestamps**, not its slug, so an upstream naming change does not silently empty
the page. Per market: price, bid, ask, mid, spread, order-book ladder and depth
chart, liquidity, volume, 24 h volume, price change, live countdown, settlement
time, status, tick size, minimum size.

### Indicators

EMA 9 / 21 / 50, RSI, MACD (line, signal, histogram), ATR, VWAP, Bollinger Bands
(+ %B and bandwidth), volume delta, momentum, realised volatility and ADX trend
strength.

> Polymarket publishes no OHLCV for its markets, so the dashboard builds candles
> itself by sampling each market's mid price every `CANDLE_INTERVAL_S` (default
> 15 s) and accumulating observed trade prints as volume. Every indicator is
> computed from that observed series — nothing is imputed or back-filled. A value
> reads `—` until its window is full rather than showing a misleading zero.

### Strategies, orders, trades, analytics

Per strategy: enabled, running, last and current signal, position, entry, PnL,
win rate, average win/loss, trade count, profit factor, accuracy and a
performance rank.

> The shipped compiled strategy does not publish confidence, take-profit or
> stop-loss levels, so those columns show `not published` rather than a made-up
> number. If a strategy exposes `confidence_for(market_id)`,
> `stop_loss_for(...)` or `take_profit_for(...)`, the dashboard picks them up
> automatically.

Analytics: total/winning/losing trades, win and loss rate, average trade, average
holding time, profit factor, Sharpe, Sortino, Calmar, max drawdown, largest win
and loss, current and longest streaks, expectancy, average R multiple and
risk/reward — plus daily, weekly and monthly PnL and per-strategy/per-market
breakdowns.

> Sharpe and Sortino are annualised from **daily** equity returns and read `—`
> until at least two days of samples exist. A ratio computed from ten minutes of
> data is noise, not information.

Fourteen interactive charts (equity curve, portfolio growth, balance history,
rolling win rate, daily/weekly/monthly PnL, drawdown, trade distribution, trade
timeline, market performance, strategy performance, order flow, allocation) are
drawn on canvas by a ~600-line local library — no CDN, no framework.

### Journal, VPS, alerts

Every completed round trip is journalled automatically with the facts filled in;
you add entry/exit reason, confidence, notes, tags, a 1–5 rating and optional
screenshots. Import is idempotent and never overwrites what you have written.

VPS panel: CPU (total and per core, load averages), memory, swap, disk, network
throughput and errors, VPS uptime, per-service uptime, restart counts, memory per
unit, database sizes and log sizes.

Alerts fire for trade executed, trade closed, profit target, stop loss, order
rejected, low balance, allowance problem, API failure, websocket disconnect,
engine gone silent, high CPU/memory/disk, service failed, strategy errors and the
daily loss limit. Delivery: browser (in-page toast + Web Notifications), email,
Telegram and Discord. Each `(kind, mode)` is deduplicated within a cooldown
window, so a condition that persists for an hour produces one alert, not
thousands.

---

## 4. Controls

Start / stop / restart each engine, restart the dashboard, rebuild the live API
client, reconnect the websocket, refresh market data, back up databases, export
trades / orders / journal as CSV or JSON, and **emergency stop**.

Emergency stop writes the kill switch *first*, waits for the engine to cancel its
open orders, and only then stops the unit. Stopping the unit alone would leave
resting orders on the book — which is why the button does not do that.

All service control goes through `sudo -n systemctl <verb> <unit>` with a
hardcoded allowlist of three units and three verbs. No caller-supplied string
ever reaches a shell; `subprocess` is invoked with an argument list and
`shell=False`. If the sudoers rule is missing, buttons fail loudly with a pointer
to `deploy/polybot-dashboard.sudoers` rather than hanging on a password prompt.

---

## 5. Security

| Control | Implementation |
|---|---|
| Authentication | bcrypt (cost 12); constant-time comparison against a dummy hash for unknown users so login timing cannot enumerate accounts |
| Sessions | opaque 256-bit server-side tokens, `HttpOnly` + `SameSite=Lax` + `Secure`, absolute TTL and idle timeout, revocable instantly |
| CSRF | every unsafe method requires `X-CSRF-Token` matching the session; a cookie alone is never sufficient |
| Roles | `admin` / `trader` / `viewer`, enforced server-side per route — the UI hides what a role cannot do, but hiding is not the control |
| Brute force | per-user and per-IP lockout after `DASHBOARD_LOGIN_MAX_ATTEMPTS`, plus an nginx rate limit |
| Transport | TLS at nginx (or directly), HSTS, strict CSP with no external origins, frame-deny, nosniff |
| WebSocket | `Origin` validated against the configured public URL before the upgrade |
| Secret masking | log lines, audit details and the config endpoint are passed through `mask_secrets()`, which redacts 64-hex keys and any `*secret*`/`*token*`/`*password*`/`*passphrase*` assignment |
| Audit | append-only log of every privileged action with actor, role, target, IP and outcome |
| Path safety | log reads are restricted to `*.log` inside the mode's log directory; journal screenshots resolve inside the uploads directory only |

Passwords must be 12+ characters using at least three character classes. The last
active admin cannot be deleted, demoted or disabled.

---

## 6. Performance

Sized for 2 vCPU / 4 GB / 80 GB SSD.

* The 1 Hz websocket snapshot is **lean** — latest values only, no order-book
  ladders, no indicator series. Order books, indicator history, analytics, logs
  and the journal are fetched on demand when a panel needs them.
* Snapshot assembly runs in a worker thread, never on the event loop.
* Telemetry writes are queued and batched by a single background thread; the
  trading engines never block on disk.
* SQLite runs in WAL mode: the dashboard reads while an engine writes, with no
  locking between them, and the reader opens the file `mode=ro`.
* Clients that cannot keep up are disconnected rather than allowed to grow an
  unbounded send buffer.
* Hourly pruning bounds audit, notification, login-attempt, session and candle
  tables. Backups keep the newest 14 sets.
* Memory ceilings: paper 600 MB, live 700 MB, dashboard 700 MB.

---

## 7. Operating notes

**Where things live**

```
data/paper/{engine.db,logs/,state/}      paper trading, nothing else
data/live/{engine.db,logs/,state/}       live trading, nothing else
data/dashboard/{dashboard.db,backups/,uploads/}
```

**The live engine's logs moved.** They are now `data/live/logs/` rather than
`logs/`, so paper and live can never write to the same file. `LOG_DIR` and
`STATE_DIR` still override if you need the old layout.

**Useful commands**

```bash
journalctl -u polybot-dashboard -f          # dashboard
journalctl -u polybot-live -f               # live engine
tail -f data/live/logs/execution.log        # engine detail
sqlite3 data/live/engine.db 'select count(*) from trades;'
systemctl status polybot-paper polybot-live polybot-dashboard
```

**Recovering a locked-out admin**

```bash
sudo -u polybot /opt/polybot/.venv/bin/python - <<'PY'
from dashboard.config import DashboardSettings
from dashboard.db import DashboardDB
from dashboard.security import SecurityManager
s = DashboardSettings.load()
db = DashboardDB(s.db_path)
sec = SecurityManager(db, session_ttl_s=s.session_ttl_s, idle_timeout_s=s.session_idle_timeout_s)
user = db.query_one("SELECT id FROM users WHERE username='admin'")
sec.change_password(user["id"], "YourNewStrongPassword123!")
db.execute("DELETE FROM login_attempts")
print("reset")
PY
```

---

## 8. Known limits

* **Confidence, stop-loss and take-profit are not shown for the shipped
  strategy** because it does not publish them. The plumbing is there for a
  strategy that does.
* **Indicators are computed over market mid-price candles**, not spot BTC/ETH
  prices — Polymarket does not provide the latter. This is the honest reading of
  the market you are actually trading.
* **Fees** are read from whatever the exchange reports on a fill. Polymarket
  charges none on most markets, so this usually reads 0.
* **Margin usage** is not shown: Polymarket markets are fully collateralised, so
  there is no margin to report. Locked collateral and buying power cover the
  equivalent ground.
* **"VPS offline"** is detected by the browser losing the dashboard websocket
  (the dashboard cannot report its own host being down). Engine-level silence is
  detected server-side via heartbeat age.
* Live market data and live account sync were validated against the API contract
  and normalise several response shapes defensively, but could not be exercised
  against the production API from the build environment — it has no egress. Watch
  the Wallet and Markets panels on first run; both surface the exact upstream
  error when a call fails.
