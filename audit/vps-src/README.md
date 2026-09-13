# Polymarket Live Trading Bot

A production-structured, async live-trading engine for Polymarket's CLOB, built
**around the shipped compiled strategy** `Strategy/s1_cheap_buy.cpython-313.pyc`.
The strategy bytecode is never modified, rewritten, or reimplemented — the engine
provides the framework it expects and executes it as the sole signal source.

> ⚠️ **Safety first.** The bot runs in **dry-run by default** (orders are simulated,
> never submitted). Real orders are only sent when you explicitly set
> `ENABLE_LIVE_TRADING=true`. It trades **real money** in live mode. Start in
> dry-run, read the logs, then arm live trading deliberately.

> 🔐 **Secrets.** `env.txt` currently contains a real private key and API secrets in
> plaintext. It is git-ignored by this repo, but you should treat that key as
> **exposed** and rotate/move it to a secrets manager. Never commit `env.txt`.

> 🔁 **CLOB V2.** The bot targets Polymarket's **CLOB V2** exchange via
> `py-clob-client-v2`. The archived v1 `py-clob-client` (≤0.34.x) signs orders
> against the old EIP-712 domain (version 1) and is rejected with
> `invalid order version` since the V2 migration (2026-04-28). Programmatic
> order placement also requires the funder wallet to have been enabled via the
> Polymarket **deposit / "Enable trading" flow** — otherwise the CLOB returns
> `maker address not allowed, please use the deposit wallet flow`.

---

## The strategy (S1 — "1c Buy, Ultra-Cheap Dislocation")

Reconstructed from the bytecode's own docstring and interface (not reimplemented):
rest bids at **1¢ / 2¢ / 3¢ on both outcome sides** after a short entry delay,
take profit in tiers (**50% at 10¢, 100% at 15¢**), and **cancel any unfilled
orders inside the last 30s** before the market closes.

The engine binds to the exact contract the bytecode imports:

| Bytecode needs (relative import) | Provided by |
| --- | --- |
| `from ..market_data import MarketState` → `polybot.market_data` | `polybot/strategy/market_data.py` (aliased) |
| `from .base import Strategy, MAX_TRADE_VALUE, shares_for_notional` → `polybot.strategies.base` | `polybot/strategy/base.py` (aliased) |
| `self.broker.{place_limit, sell, position_qty, cancel_all_open, notional_in_market}` | `polybot/execution/executor.py::Broker` |
| `self.risk.notional_ok(current, limit)` | `polybot/risk/engine.py::RiskEngine` |
| `self.market_state(id)`, `self.notional_in_market(id)` | `polybot/strategy/base.py::Strategy` |

`polybot/strategy/loader.py` maps our modules onto the dotted names the bytecode
resolves and executes it via the standard sourceless loader.

---

## Architecture

```
run.py  ──►  polybot/main.py (TradingBot)
                  │  startup validation → wire → supervise tasks → graceful shutdown
   ┌──────────────┼───────────────────────────────────────────────┐
   ▼              ▼                        ▼                        ▼
config/        exchange/               execution/               risk/
  settings       auth  (L1/L2 creds)     executor (Broker)        engine (limits,
exchange/        clob  (async SDK)        strategy_runner (ticks)   kill switch,
  websocket ◄── market_registry ──► strategy/ (compiled .pyc      emergency stop)
  (market+user   (MarketState)          + base + market_data)
   channels)         │                        │
                     ▼                        ▼
                 orders/manager (place/cancel/replace, dedup,   portfolio/manager
                 fills, reconcile, recovery, accounting)        (positions, PnL)
                     │                                          wallet/balance
                     ▼                                          monitoring/ (health,
                 api/rest_client (httpx, retries, 401/403/429/5xx)   metrics)
                 api/camber (optional, env-token only)          logging/ (multi-file)
```

**Flow:** the `market` websocket (with a REST poll backstop) keeps each
`MarketState` fresh → `StrategyRunner` ticks `on_bar` in a worker thread → the
compiled strategy calls the `Broker` → intents pass the `RiskEngine` gate and are
recorded with optimistic notional accounting → the async `OrderManager` submits
them (or simulates in dry-run) → the `user` websocket + poll reconcile fills into
the `PortfolioManager`.

---

## Setup

```bash
cd /home/akash/Bot-s1
python3.13 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt          # installs py-clob-client-v2, httpx, websockets
cp env.example.txt env.txt                          # then edit env.txt (already present here)
# set at minimum:  MARKETS=<condition_id>[,<condition_id>...]
```

`env.txt` keys already present: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_PROXY_ADDRESS`,
`POLY_BUILDER_API_KEY/SECRET/PASSPHRASE`. See `env.example.txt` for every option.

---

## Running

```bash
# 1) Dry-run (default, safe): connects, ticks the strategy, simulates orders
.venv/bin/python run.py

# 2) Live (real funds) — only after reviewing dry-run logs
ENABLE_LIVE_TRADING=true .venv/bin/python run.py

# Kill switch (halts new orders + cancels open orders, no restart needed):
touch state/KILL_SWITCH          # remove the file to resume

# Graceful stop: Ctrl-C (SIGINT) or SIGTERM
```

On startup the bot runs a **6-step validation** and aborts with a descriptive
error (exit code 2) if any step fails: `[1]` environment/credentials →
`[2]` API access (auth + `get_ok`) → `[3]` wallet balance → `[4]` compiled
strategy loads → `[5]` market connectivity → `[6]` component wiring + order
recovery.

---

## Risk controls (all configurable in `env.txt`)

Max order value, max position size, max total exposure, max concurrent markets,
max open orders, per-market cooldown, max daily loss (→ auto emergency-stop),
duplicate-order suppression, wallet-balance validation, a filesystem **kill
switch**, and a programmatic **emergency stop**. Every order intent — regardless
of what the strategy itself checks — passes the full `RiskEngine.check_order`
gate before submission.

## Logging & monitoring

Structured, timestamped logs under `logs/`: `trade.log`, `orders.log`,
`strategy.log`, `execution.log`, `websocket.log`, and an aggregated `errors.log`
(WARNING+). Set `LOG_JSON=true` for one-JSON-object-per-line. A heartbeat logs
wallet/balance/open-orders/positions/PnL/connection-status/latency every
`HEARTBEAT_INTERVAL_S`. On shutdown a snapshot is written to
`state/last_state.json`.

---

## Tests

No network required — the live API is stubbed:

```bash
.venv/bin/python -m tests.test_strategy_wiring    # compiled strategy: entry/TP/cancel/no-op
.venv/bin/python -m tests.test_engine_offline     # Broker→OrderManager→Risk→Portfolio, dry-run
.venv/bin/python -m tests.test_startup_offline     # full TradingBot startup→tick→shutdown
```

---

## Assumptions & notes

- **Credential mapping.** L2 trading creds are **derived from the private key** at
  startup unless you set explicit `POLY_API_KEY/SECRET/PASSPHRASE`. If your
  `POLY_BUILDER_*` are actually your L2 trading creds, set them as `POLY_API_*`
  instead. In **CLOB V2**, builder-program *attribution* is a `builder_config`
  built from `POLY_BUILDER_ADDRESS` + `POLY_BUILDER_CODE` (an on-chain address and
  a 32-byte code embedded in the order) — set both to enable it; the legacy
  `POLY_BUILDER_API_KEY/SECRET/PASSPHRASE` now only authenticate the separate
  builder-reporting API, not order signing.
- **`SIGNATURE_TYPE`** defaults to `1` (Polymarket proxy wallet) because the env
  provides `POLYMARKET_PROXY_ADDRESS`. Use `0` for a plain EOA, `2` for a Safe,
  `3` for a deposit wallet.
  Under CLOB V2, whichever signer/funder you use must first be enabled for trading
  via the Polymarket deposit flow, or the exchange rejects orders with
  `maker address not allowed`.
- **`PER_ORDER_NOTIONAL`** (framework constant, not shipped) defaults to
  `MAX_TRADE_VALUE / 3` so the strategy's documented 1¢/2¢/3¢ ladder rests on both
  sides within its `2 × MAX_TRADE_VALUE` cap. Tune via env.
- **`MARKETS`** must be CLOB `condition_id`s. Outcome-token index 0 → `up`, 1 →
  `down`.
- **Camber API** is optional and inert unless both `CAMBER_API_TOKEN` and
  `CAMBER_API_URL` are set (token read from env only, sent as a Bearer header).
- **Websocket message shapes** follow Polymarket's documented `market`/`user`
  channels; the REST poll is the reconciliation backstop if a shape differs.
```
