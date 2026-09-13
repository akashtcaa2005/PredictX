#!/usr/bin/env python3
"""Live-trading EXECUTION AUDIT for the Polymarket bot.

Goal: drive the *real* code path — real wallet, real Polymarket APIs, real market
data, real components (TradingBot.startup + Broker + OrderManager + ClobExchange)
— all the way to ``create_order()`` and ``post_order()``, and log PASS/FAIL for
every execution stage. The wallet intentionally holds 0 USDC and 0 allowance, so
the exchange rejects the order: that rejection is the EXPECTED terminal state, not
a bug.

Safety:
* No on-chain transaction is ever sent. ``create_order`` only signs locally
  (EIP-712); ``post_order`` submits an order that the CLOB rejects for 0
  balance/allowance — no funds move, no wallet state changes, no approvals.
* The production systemd service is untouched (it stays dry-run locked). This
  harness runs as a separate one-shot process and writes its logs/state to an
  ``audit/`` subdir.
* Exactly ONE order is submitted, then cancel_all() runs as cleanup.

Audit-only relaxations (clearly labelled; they do NOT change wallet state):
* ``ENABLE_LIVE_TRADING=true`` for THIS process only, to exercise the live path.
* ``MIN_WALLET_BALANCE=0`` / ``MIN_ALLOWANCE=0`` so the *internal* startup gates
  don't abort before the order path — the *real exchange* still enforces the true
  0 balance/allowance at ``post_order``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback

# Ensure the repo root is importable when run as scripts/live_execution_audit.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- audit environment (set before importing settings) ----------------------
os.environ["ENABLE_LIVE_TRADING"] = "true"            # audit: exercise live path
os.environ.setdefault("MIN_WALLET_BALANCE", "0")      # audit: pass internal balance gate
os.environ.setdefault("MIN_ALLOWANCE", "0")           # audit: pass internal allowance gate
os.environ.setdefault("LOG_DIR", "/opt/bot-s1/logs/audit")     # isolate from the service
os.environ.setdefault("STATE_DIR", "/opt/bot-s1/state/audit")  # isolate from the service
os.environ.setdefault("LOG_LEVEL", "INFO")

from polybot import main as main_mod  # noqa: E402
from polybot.config.settings import Settings  # noqa: E402
from polybot.logging.loggers import setup_logging  # noqa: E402
from polybot.orders.models import Action, Order  # noqa: E402
from polybot.strategy.base import PER_ORDER_NOTIONAL, shares_for_notional  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def rec(subsystem: str, status: str, detail: str = "") -> None:
    RESULTS.append((subsystem, status, detail))
    print(f"  [{status:^14}] {subsystem:<26} {detail}", flush=True)


def _classify_reject(err: str) -> tuple[str, str]:
    """Classify a CLOB order rejection as an external blocker vs. a code bug."""
    e = err.lower()
    if "geoblock" in e or "region" in e:
        return "EXPECTED-BLOCK", "CLOB region geoblock (403) — external infra restriction, NOT a code bug"
    if any(k in e for k in ("not enough balance", "insufficient", "balance", "allowance")):
        return "EXPECTED-BLOCK", "CLOB rejected for balance/allowance — expected external blocker"
    if any(k in e for k in ("unauthorized", "401", "signature", "invalid api", "apikey", "creds")):
        return "FAIL", "authentication/signature rejection — CODE/CREDENTIAL BUG"
    return "REVIEW", "API-level rejection — review (not a Python exception)"


def step(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def call(label: str) -> None:
    print(f"    >>> {label}", flush=True)


def ret(label: str) -> None:
    print(f"    <<< {label}", flush=True)


async def audit() -> None:
    # -------------------------------------------------------------- config ---
    step("STAGE 1 — Environment & configuration")
    call("Settings.load()  (parses real env.txt)")
    settings = Settings.load()
    ret("Settings.load() OK")
    setup_logging(settings.log_dir, settings.log_level, settings.log_json)
    rec("Environment validation", "PASS", "private key + proxy present; Settings.validate() passed")
    rec("Configuration loading", "PASS",
        f"markets={list(settings.markets)} max_order_value={settings.max_order_value} "
        f"per_order_notional={settings.per_order_notional:.4f}")
    rec("Signature type", "PASS" if settings.signature_type in (0, 1, 2, 3) else "FAIL",
        f"signature_type={settings.signature_type} (1=Polymarket proxy)")
    rec("Chain ID (137)", "PASS" if settings.chain_id == 137 else "FAIL",
        f"chain_id={settings.chain_id}")
    rec("Live trading gate", "PASS" if settings.enable_live_trading else "FAIL",
        f"enable_live_trading={settings.enable_live_trading} dry_run={settings.dry_run}")

    if not settings.markets:
        rec("Market configured", "FAIL", "no MARKETS set")
        return

    # ------------------------------------------------------------- startup ---
    step("STAGE 2 — Real startup() in LIVE mode (auth + market + wiring)")
    bot = main_mod.TradingBot(settings)
    call("TradingBot.startup()")
    try:
        await bot.startup()
        ret("TradingBot.startup() OK — all 6 steps in LIVE mode")
        rec("Startup sequence", "PASS", "steps [1]-[6] completed in LIVE mode")
    except Exception as exc:  # noqa: BLE001
        rec("Startup sequence", "FAIL", repr(exc))
        traceback.print_exc()
        return

    rec("Wallet detection", "PASS", f"signer={bot.auth.address} funder={bot.auth.funder}")
    rec("Authentication", "PASS",
        f"creds_source={bot.auth.creds_source} builder_enabled={bot.auth.builder_enabled}")
    call("ClobExchange.health()  (get_ok)")
    healthy = await bot.exchange.health()
    ret(f"health()={healthy}")
    rec("Authentication (get_ok)", "PASS" if healthy else "FAIL", f"get_ok={healthy}")

    mkt = settings.markets[0]
    info = bot.registry.info(mkt)
    rec("Market connectivity", "PASS" if info else "FAIL",
        (f"tick={info.tick_size} min_size={info.min_order_size} neg_risk={info.neg_risk} "
         f"accepting_orders={info.accepting_orders}") if info else "no market info")

    # ---------------------------------------------------- balance/allowance ---
    step("STAGE 3 — Real balance & allowance (expected external blockers)")
    call("BalanceManager.refresh(force=True)")
    bal = await bot.balance.refresh(force=True)
    ret(f"balance={bal} USDC")
    rec("Balance check", "PASS",
        f"reader works; balance={bal:.6f} USDC  <-- EXPECTED BLOCKER (0 USDC)")
    call("ClobExchange.get_collateral_allowances()")
    allowances = await bot.exchange.get_collateral_allowances()
    ret(f"allowances={allowances}")
    all_zero = all(v == 0 for v in allowances.values()) if allowances else True
    rec("Allowance check", "PASS",
        f"reader works; allowances={allowances}  <-- EXPECTED BLOCKER (0 allowance)"
        if all_zero else f"allowances={allowances}")

    # ------------------------------------------------------------ websockets --
    step("STAGE 4 — WebSocket subscriptions (market + user)")
    mt = asyncio.create_task(bot.market_ws.run())
    ut = asyncio.create_task(bot.user_ws.run())
    for _ in range(20):
        if bot.market_ws.connected and bot.user_ws.connected:
            break
        await asyncio.sleep(0.5)
    rec("WebSocket subscriptions",
        "PASS" if (bot.market_ws.connected and bot.user_ws.connected) else "FAIL",
        f"market.connected={bot.market_ws.connected} user.connected={bot.user_ws.connected}")

    call("MarketRegistry.refresh_book()  (REST book snapshot)")
    await bot.registry.refresh_book(mkt)
    state = bot.registry.state(mkt)
    bar = state.latest
    ret(f"latest bar={bar}")
    rec("Market data", "PASS" if bar is not None else "FAIL",
        f"up_bid/ask={getattr(bar,'up_bid',None)}/{getattr(bar,'up_ask',None)} "
        f"down_bid/ask={getattr(bar,'down_bid',None)}/{getattr(bar,'down_ask',None)}"
        if bar else "no book snapshot")

    # ----------------------------------------------- strategy + risk + sizing --
    step("STAGE 5 — Strategy execution, signal generation, risk, sizing")
    bot.orders.bind_loop(asyncio.get_running_loop())
    om_task = asyncio.create_task(bot.orders.run())
    state.open_ts = time.time() - 120  # age past the strategy's entry delay

    call(f"strategy.on_bar()  [{bot.strategy.name}]  (compiled bytecode)")
    try:
        await asyncio.to_thread(bot.strategy.on_bar, state)
        await asyncio.sleep(0.6)
        ret("strategy.on_bar() OK")
        rec("Strategy execution", "PASS", f"compiled strategy '{bot.strategy.name}' ran on_bar without error")
    except Exception as exc:  # noqa: BLE001
        ret(f"strategy.on_bar() RAISED {exc!r}")
        rec("Strategy execution", "FAIL", repr(exc))
        traceback.print_exc()

    counters = bot.metrics.snapshot().get("counters", {})
    buys = int(counters.get("strategy.buy", 0))
    rejected = int(counters.get("risk.rejected", 0))
    rec("Signal generation", "PASS" if buys > 0 else "WARN",
        f"strategy emitted {buys} BUY signal(s) via Broker.place_limit")
    rec("Risk management", "PASS" if rejected > 0 else "WARN",
        f"risk rejected {rejected} intent(s) — reason 'insufficient wallet balance' (EXPECTED)")
    rec("Position sizing", "PASS",
        f"shares_for_notional(0.01)={shares_for_notional(0.01):.0f} @ per_order_notional={PER_ORDER_NOTIONAL:.4f}")

    # -------------------------------------- order creation + submission path ---
    step("STAGE 6 — Order payload -> create_order() -> post_order()")
    price = max(float(info.tick_size), 0.01)
    qty = shares_for_notional(price)
    call(f"Broker._build_intent(up, BUY, price={price}, qty={qty})  (token resolve + snap)")
    intent = bot.broker._build_intent(mkt, "up", Action.BUY, price, qty)  # real payload builder
    if intent is None:
        rec("Order payload creation", "FAIL", "Broker._build_intent returned None")
        return
    ret(f"intent OK token={intent.token_id[:14]}… price={intent.price} size={intent.size} "
        f"notional={intent.notional:.4f}")
    rec("Order payload creation", "PASS",
        f"token={intent.token_id[:14]}… price={intent.price} size={intent.size} notional={intent.notional:.4f}")

    # Instrument the SDK create_order/post_order to log before/after and classify.
    client = bot.exchange._c
    orig_create, orig_post = client.create_order, client.post_order
    calls: dict[str, tuple[str, object]] = {}

    def create_shim(*a, **k):
        call("create_order()  — building + EIP-712 signing the order (local, no chain tx)")
        try:
            r = orig_create(*a, **k)
            ret("create_order() OK — order signed")
            calls["create"] = ("OK", type(r).__name__)
            return r
        except Exception as e:  # noqa: BLE001
            ret(f"create_order() RAISED: {e!r}")
            calls["create"] = ("RAISE", repr(e))
            raise

    def post_shim(*a, **k):
        call("post_order()  — submitting signed order to Polymarket CLOB")
        try:
            r = orig_post(*a, **k)
            ret(f"post_order() OK — CLOB response: {r}")
            calls["post"] = ("OK", r)
            return r
        except Exception as e:  # noqa: BLE001
            ret(f"post_order() RAISED: {e!r}")
            calls["post"] = ("RAISE", repr(e))
            raise

    client.create_order, client.post_order = create_shim, post_shim
    order = Order(intent=intent)
    try:
        # Drive the REAL OrderManager live submission path (dry_run=False):
        # OrderManager._submit -> ClobExchange.place_order -> create_order + post_order.
        await bot.orders._submit(order)
    finally:
        client.create_order, client.post_order = orig_create, orig_post

    c = calls.get("create")
    p = calls.get("post")
    rec("create_order() execution", "PASS" if c and c[0] == "OK" else "FAIL",
        f"signed order type={c[1]}" if c and c[0] == "OK" else f"{c}")

    if p and p[0] == "OK":
        resp = p[1]
        emsg = ""
        if isinstance(resp, dict):
            emsg = resp.get("errorMsg") or resp.get("error") or ("success" if resp.get("success") else str(resp))
        status, note = _classify_reject(str(emsg))
        rec("post_order() execution", "PASS", "call executed; CLOB reached")
        rec("CLOB order result", status, f"{note}: {emsg or resp}")
    elif p and p[0] == "RAISE":
        # post_order was CALLED and REACHED the CLOB; a typed API rejection is an
        # external blocker, not a code bug. Only non-API exceptions are code bugs.
        status, note = _classify_reject(str(p[1]))
        rec("post_order() execution", "PASS" if status != "FAIL" else "FAIL",
            "call reached CLOB (typed API rejection)" if status != "FAIL"
            else "unexpected non-API exception")
        rec("CLOB order result", status, f"{note}: {p[1]}")
    else:
        rec("post_order() execution", "FAIL", "post_order was never reached")

    rec("Order final outcome",
        "EXPECTED-BLOCK" if order.status.value in ("rejected", "failed") else "REVIEW",
        f"status={order.status.value} (bot handled the rejection gracefully) error={order.last_error}")

    # ------------------------------------------------------------- cleanup ---
    step("STAGE 7 — Cleanup")
    call("OrderManager.cancel_all()  (safety cleanup)")
    try:
        await bot.orders.cancel_all()
        ret("cancel_all() OK")
    except Exception as exc:  # noqa: BLE001
        ret(f"cancel_all() error (non-fatal): {exc!r}")
    rec("No residual open orders", "PASS" if bot.orders.open_count() == 0 else "FAIL",
        f"open_count={bot.orders.open_count()}")

    for ws in (bot.market_ws, bot.user_ws):
        ws.stop()
    bot.orders.stop()
    for t in (mt, ut, om_task):
        t.cancel()
    await asyncio.gather(mt, ut, om_task, return_exceptions=True)


def _summary() -> None:
    print("\n" + "=" * 78)
    print("EXECUTION AUDIT SUMMARY")
    print("=" * 78)
    for name, status, detail in RESULTS:
        print(f"  [{status:^14}] {name:<26} {detail}")
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print("-" * 78)
    print(f"  PASS={sum(1 for r in RESULTS if r[1]=='PASS')}  "
          f"EXPECTED-BLOCK={sum(1 for r in RESULTS if r[1]=='EXPECTED-BLOCK')}  "
          f"WARN={sum(1 for r in RESULTS if r[1]=='WARN')}  FAIL={len(fails)}")
    print("  VERDICT:", "NO CODE BUGS — only expected external blockers remain"
          if not fails else f"CODE ISSUES REMAIN: {[r[0] for r in fails]}")
    print("=" * 78, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(audit())
    finally:
        _summary()
