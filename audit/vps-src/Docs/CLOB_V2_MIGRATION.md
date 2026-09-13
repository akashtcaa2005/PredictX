# CLOB V2 SDK Migration Notes

**Date:** 2026-08-07
**Reason:** Polymarket migrated the CLOB exchange to **V2** on **2026-04-28**,
bumping the EIP-712 exchange domain version from `1` to `2`. The archived
`py-clob-client` (≤ `0.34.6`) signs version-`1` orders, which the V2 exchange
rejects with:

```
HTTP 400  {"error":"invalid order version, please use the latest clob-client"}
```

There is **no backward compatibility**; the fix is to move to the V2 SDK.

## Dependency change

| | Before | After |
| --- | --- | --- |
| CLOB SDK | `py-clob-client==0.34.6` | `py-clob-client-v2>=1.1.0` |
| Builder signing | `py-builder-signing-sdk==0.0.2` | removed (builtin `BuilderConfig` in V2) |
| Transitive pins | `web3>=6`, `eth-account>=0.11` | `eth-account>=0.13`, `poly-eip712-structs`, `py-order-utils` |
| HTTP | `httpx>=0.27` | `httpx[http2]>=0.27` |

`requirements.txt` was updated accordingly; `py-clob-client` and
`py-builder-signing-sdk` were uninstalled from `.venv`.

## API changes absorbed by the exchange layer

All SDK usage is encapsulated in `polybot/exchange/auth.py` and
`polybot/exchange/clob.py`; the rest of the bot talks to the `ClobExchange`
facade and needed no changes.

| Concern | v1 (`py_clob_client`) | v2 (`py_clob_client_v2`) |
| --- | --- | --- |
| Import root | `py_clob_client` | `py_clob_client_v2` |
| Client ctor | `ClobClient(host, chain_id, key, creds, signature_type, funder, builder_config)` | same signature |
| Derive L2 creds | `create_or_derive_api_creds()` | `create_or_derive_api_key()` |
| Order args | `OrderArgs(token_id, price, size, side)` | same (now `OrderArgsV2`, side is a str; `BUY`/`SELL` unchanged) |
| Create + sign | `create_order(args, options)` | same (returns a `SignedOrderV2`) |
| Post | `post_order(signed, order_type)` | same |
| Order options | `PartialCreateOrderOptions(tick_size, neg_risk)` | same; tick ladder adds `0.005`, `0.0025` |
| List open orders | `get_orders(OpenOrderParams)` | **`get_open_orders(OpenOrderParams)`** |
| Cancel one | `cancel(order_id)` | **`cancel_order(OrderPayload(orderID=...))`** |
| Cancel many | `cancel_orders([ids])` | same |
| Cancel all | `cancel_all()` | same |
| Cancel by market | `cancel_market_orders(market, asset_id)` | **`cancel_market_orders(OrderMarketCancelParams(market=, asset_id=))`** |
| Balances/allowance | `get_balance_allowance` / `update_balance_allowance(BalanceAllowanceParams)` | same (`AssetType`, `signature_type` unchanged) |
| Market metadata | `get_market` / `get_order_book` / `get_tick_size` / `get_neg_risk` / `get_ok` / `get_server_time` | unchanged |
| Signature types | 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271 | unchanged (`SignatureTypeV2`) |

### Builder-program attribution

V2 changed the builder model. Attribution is now an on-chain **builder address**
plus a **32-byte builder code** carried in the order via
`BuilderConfig(builder_address, builder_code)` — not the legacy builder API
creds. New optional settings `POLY_BUILDER_ADDRESS` / `POLY_BUILDER_CODE`
(`builder_address` / `builder_code`) enable it; when unset, attribution is
disabled (it is strictly optional and unrelated to whether orders are accepted).
The legacy `POLY_BUILDER_*` creds now only authenticate the separate
builder-reporting API and are no longer used for signing.

## Verification (run on the VPS)

* `pytest tests/` → **14 passed** (`FakeClient` stub updated to expose the V2
  `get_open_orders`).
* `scripts/preflight.py` → **PREFLIGHT PASSED**.
* `scripts/live_execution_audit.py` (real wallet, real CLOB, one order) →
  `create_order()` produced a **`SignedOrderV2`** and `post_order()` reached the
  live CLOB. **No `invalid order version`**, no signing/auth/serialization
  errors. Verdict: **PASS=20, EXPECTED-BLOCK=2, WARN=2, FAIL=0 — NO CODE BUGS**.

## Remaining external blocker (not a code bug)

The audit wallet is rejected at `post_order` with:

```
HTTP 400  {"error":"maker address not allowed, please use the deposit wallet flow"}
```

Root cause: under CLOB V2, a wallet (here `SIGNATURE_TYPE=1`, a Magic.link-derived
Polymarket proxy) must be enabled for trading via the Polymarket **deposit /
"Enable trading" flow** before it can place orders programmatically. The audit
wallet holds 0 USDC / 0 allowance and has not been enabled — the same class of
external, account-provisioning blocker as the pre-existing 0-balance/0-allowance
condition. To go live: fund the funder wallet and enable trading for it in the
Polymarket UI, then re-run the audit. The audit classifier now reports this (and
any `invalid order version` regression) explicitly.

## Maker / deposit-wallet validation (added 2026-08-08)

**Error addressed:** `HTTP 400 {"error":"maker address not allowed, please use the deposit wallet flow"}`.

**Root cause (verified from the SDK + live API):** CLOB V2 builds every order with
`maker = funder` and the signature produced by the signer key
(`py_clob_client_v2.order_builder.builder` → `maker=self.funder`,
`signatureType=self.signature_type`). This bot signs with key
`signer=0x2918…` and sets `maker = POLYMARKET_PROXY_ADDRESS = 0x0f88…`
(`signature_type=1`, POLY_PROXY). The CLOB rejects that maker because it is not
an **enabled deposit wallet** for the account. Verified facts that *rule out*
other causes: `get_api_keys()` returns the active key → the L2
api_key/secret/passphrase belong to the **same account** as the signer; and
`get_closed_only_mode()` → `false` → the account is not banned. So this is an
account-provisioning matter (the deposit wallet must complete Polymarket's
deposit / "Enable trading" flow), not a credential, ban, or code/signing bug.

**What the bot now does** (`polybot/wallet/maker.py::MakerWalletValidator`,
startup step `[2b/6]`, also surfaced in `scripts/preflight.py` `[5/5]`):

* logs the exact **maker/deposit wallet** every order will be signed for;
* verifies the L2 creds belong to the signer's account (`get_api_keys` contains
  the active `api_key`);
* verifies a funder/maker is configured and — for smart-contract signature types
  (POLY_PROXY / POLY_GNOSIS_SAFE / POLY_1271) — is distinct from the bare signer
  EOA (otherwise the maker would silently default to the EOA);
* verifies the account is not in closed-only / ban mode;
* in **live** mode, a hard failure raises `MakerWalletError → StartupError` so
  startup aborts **before any order is signed**; in dry-run it warns only.

These are read-only, server-authoritative checks — no order is ever submitted.
Final maker allow-listing is enforced by the exchange at submission time, so a
wallet that passes these checks but has not completed the deposit/"Enable
trading" flow will still be rejected there (and the live audit reports that as an
expected external blocker).

**Operator action to actually place orders:** confirm `0x0f88…` is the signer's
Polymarket proxy/deposit wallet, then complete the deposit / "Enable trading"
flow for it in the Polymarket UI (and fund it). If `0x0f88…` is *not* this
signer's Polymarket wallet, set `POLYMARKET_PROXY_ADDRESS` to the correct deposit
wallet for the key. No code change can add a maker to the exchange allow-list.

## USDC allowance / auto-approve (added 2026-08-08)

**Error addressed:** `USDC allowance is 0` → buys rejected by the CLOB.

**Root cause (verified on-chain):** the wallet is a Polymarket **proxy**
(`signature_type=1`, POLY_PROXY). On Polygon, the funder `0x0f88…` **is a smart
contract** (has code) and the signer `0x2918…` is an EOA. Collateral (USDC) and
its allowance belong to the **proxy**, so a raw `USDC.approve()` signed by the
signer EOA would set a *different* owner's allowance and the CLOB would still read
the proxy's allowance as 0. Additionally both wallets hold **0 POL**, so the bot
cannot broadcast any transaction. Proxy allowances are granted **gaslessly through
the proxy** via Polymarket's "Enable trading" relayer/UI — the bot cannot replicate
that on-chain.

**Changes:**
* `polybot/wallet/allowance.py` — spender/USDC addresses are no longer hardcoded;
  `official_spenders(chain_id)` reads them from the official SDK config
  (`py_clob_client_v2.config.get_contract_config`): `exchange_v2`,
  `neg_risk_exchange_v2`, `neg_risk_adapter`, `collateral`.
* `polybot/wallet/approval.py` *(new)* — `ApprovalManager`, **wallet-type aware**:
  * reads the current per-spender allowance; if already ≥ `MIN_ALLOWANCE` it does
    nothing (idempotent, no transaction);
  * **EOA (`signature_type=0`)**: when `AUTO_APPROVE_ALLOWANCE=true` and
    `POLYGON_RPC_URL` is set, builds and sends `USDC.approve(spender, MAX)` for each
    official spender, waits for the receipt, and re-reads the on-chain allowance to
    confirm; a 0-gas signer or a failed receipt raises `ApprovalError`;
  * **proxy/Safe/1271 (`signature_type=1/2/3`)**: refuses to send an incorrect
    transaction and raises `ApprovalError` directing to the gasless "Enable trading"
    flow — **no transaction is broadcast**.
* `polybot/main.py` — optional step `[3b-i]` runs `ApprovalManager.ensure_allowance()`
  before the existing allowance `verify()` when `AUTO_APPROVE_ALLOWANCE=true`. In
  **live** mode a failure raises `StartupError` (startup aborts, trading stops); in
  dry-run it warns. The existing verify-and-block behavior is unchanged.
* New settings: `AUTO_APPROVE_ALLOWANCE` (default `false`), `POLYGON_RPC_URL`,
  `MIN_ALLOWANCE`. Trading logic and risk controls are unchanged.

**Approval transaction status (this wallet):** **none sent** — correctly refused
because `signature_type=1` is a proxy wallet (and the wallet has 0 POL gas). **Final
allowance: 0.0** for all spenders. To set it, complete the deposit / "Enable trading"
flow for the proxy `0x0f88…` in the Polymarket UI (gasless). Auto-approve applies
only to EOA (`signature_type=0`) wallets.

## Production hardening (added 2026-08-08)

Infrastructure/reliability only — **no** trading, entry/exit, sizing, indicator,
or risk-calculation logic was changed.

* **WebSocket liveness (WARN-1 fixed)** — `polybot/exchange/websocket.py`: the read
  loop no longer treats a quiet socket as dead. On a `ping_interval` gap it actively
  sends a WS ping and counts the returned pong as liveness; only a ping left
  unanswered within `stale_timeout` forces a reconnect. Verified: the idle `user`
  channel held `connected=True, reconnects=0` for 70s with zero stale events (was
  reconnecting every ~45s). REST remains the fallback for order state
  (`_poll_loop`, 5s) and books (`_book_refresh_loop`, 10s).
* **Log rotation (WARN-3 fixed)** — `/etc/logrotate.d/bot-s1`: daily rotation,
  `rotate 14` (retention), `compress`, `copytruncate` (the app keeps handles open),
  `dateext`, owned by `botuser`. journald (StandardOutput=journal) self-rotates.
* **Separate log files** — `polybot/logging/loggers.py`: added dedicated
  `wallet.log` and `api.log` domains (alongside trade/orders/strategy/execution/
  websocket/errors). JSON logs remain available via `LOG_JSON=true`.
* **Monitoring feed** — `polybot/monitoring/health.py`: each heartbeat now publishes
  the live health snapshot to `state/status.json` (atomic write, real data only:
  wallet/balance/orders/positions/pnl/risk/connections/latency/ticks). A dashboard
  or probe can read this file without parsing logs.

**Not changed (by constraint / scope):** per-position stop-loss, new circuit
breaker, and max-drawdown were **not** added — they are exit/risk logic, which the
task explicitly forbids modifying. A full trade database and a metrics dashboard
(Sharpe/Sortino/profit-factor/win-rate) were not built: they require trade history
that does not exist yet (0 trades), so building them now would only produce empty
values. Existing controls remain: kill-switch file, emergency stop, daily-loss lock
→ emergency stop, max open orders, max concurrent markets, max exposure/position,
duplicate-order suppression, wallet-balance gate, maker/allowance startup gates.

## Consolidated trading-readiness gate (added 2026-08-08)

`polybot/wallet/readiness.py` (`TradingReadiness`) + startup step `[3c/6]` consolidate
the pre-trade requirements into one server-authoritative verdict and publish it to
`state/readiness.json`. Read-only; does not modify strategy/risk/execution — live
trading remains enforced by the existing `[2b]/[3]/[3b]` gates (which raise in live).

Checks + clear messaging for: authentication (L2 creds ↔ signer account via
`get_api_keys`), account not closed-only, maker/deposit-wallet coherence, USDC
allowance per official spender (`get_contract_config`), **deposit-wallet-enabled**
(defined as allowance>0 — the on-chain effect of Polymarket's gasless "Enable
Trading" flow), and USDC balance. `missing_requirements` names exactly what's absent.

Note: the gasless "Enable Trading" / deposit-wallet flow is a Polymarket **web-UI /
relayer** action and is **not exposed by the CLOB v2 SDK/API** — it cannot be invoked
programmatically, and a raw ERC-20 approve is invalid for a proxy wallet. The bot
therefore detects, reports, guides, and blocks; completing the flow is an operator
action in the Polymarket UI.
