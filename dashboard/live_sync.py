"""Read-only synchronisation with the live Polymarket account.

This is what makes the dashboard show *real* trading data rather than only what
the bot happens to have recorded: it authenticates with the same credentials the
live engine uses and continuously reads wallet balance, allowances, open orders,
fills, trade history and positions straight from Polymarket. It works whether or
not the trading engine is running.

Safety: the CLOB client built here is wrapped so only read methods are reachable.
The dashboard has no code path that can create, sign, or post an order.

Freshness: an authenticated ``user`` websocket is used as a *trigger* — when an
order or trade event arrives, the next REST poll happens immediately instead of
waiting for its interval. REST remains the source of truth, so a dropped socket
degrades to polling rather than to stale data.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from dashboard.config import DashboardSettings
from polybot.api.rest_client import RestClient
from polybot.config.settings import ConfigError, Settings
from polybot.exchange.clob import ClobExchange
from polybot.exchange.websocket import ClobWebSocket, user_subscription
from polybot.monitoring.metrics import Metrics
from polybot.utils.retry import RetryPolicy
from polybot.utils.time_utils import now_ms, now_s, utcnow_iso

log = logging.getLogger("polybot.dashboard.live_sync")

#: Methods the dashboard is allowed to call on the exchange client. Anything that
#: could create, sign, cancel or post an order is deliberately absent.
_READ_ONLY_METHODS = frozenset({
    "health", "server_time", "address", "get_market", "get_order_book", "get_tick_size",
    "get_neg_risk", "best_bid_ask", "get_orders", "get_order", "get_trades",
    "get_collateral_balance", "get_collateral_allowances",
})


class ReadOnlyExchange:
    """Attribute proxy that exposes only the read surface of :class:`ClobExchange`."""

    def __init__(self, exchange: ClobExchange) -> None:
        self._exchange = exchange

    def __getattr__(self, name: str) -> Any:
        if name not in _READ_ONLY_METHODS:
            raise AttributeError(
                f"{name!r} is not available to the dashboard: it is read-only and "
                "must never place, cancel or sign an order")
        return getattr(self._exchange, name)


class LiveAccountSync:
    """Keeps a cached, continuously refreshed view of the live account."""

    def __init__(self, settings: DashboardSettings) -> None:
        self._s = settings
        self._metrics = Metrics()
        self._retry = RetryPolicy(attempts=3, base_delay=0.5, max_delay=5.0)
        self._exchange: ReadOnlyExchange | None = None
        self._data_api: RestClient | None = None
        self._user_ws: ClobWebSocket | None = None
        self._auth: Any = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        # py_clob_client_v2 dispatches its synchronous SDK calls onto worker
        # threads that all share one module-level httpx.Client. Firing several
        # of them at once (as sync_once's asyncio.gather does) races that
        # shared client and raises "[Errno 11] Resource temporarily
        # unavailable". This lock serializes just the CLOB-touching sections
        # (health/balance/orders/trades) so at most one is ever in flight;
        # asyncio.gather still runs them concurrently at the task level, they
        # just queue for their turn on the actual network call.
        self._clob_lock = asyncio.Lock()

        self.connected = False
        self.auth_status = "not started"
        self.last_error: str | None = None
        self.last_sync_ts = 0.0
        self.last_slow_sync_ts = 0.0
        self.sync_count = 0
        self.error_count = 0

        self.state: dict[str, Any] = _empty_state()

    # ======================================================================
    # Lifecycle
    # ======================================================================
    async def start(self) -> bool:
        """Authenticate and build the read-only clients. Never raises.

        Returns True once the clients are usable. A failure here is reported in
        the UI (``auth_status``) and retried by :meth:`run`, so a network blip or
        a temporarily unreachable CLOB at boot recovers on its own.
        """
        if not self._s.live_sync_enabled:
            self.auth_status = "disabled"
            self._publish_status()
            return False
        try:
            settings = Settings.load(mode="live")
        except ConfigError as exc:
            self.auth_status = f"config error: {exc}"
            self.last_error = str(exc)
            self._publish_status()
            log.warning("live sync unavailable: %s", exc)
            return False

        try:
            from polybot.exchange.auth import build_clob_client

            self._auth = await asyncio.to_thread(build_clob_client, settings,
                                                 logging.getLogger("polybot.dashboard.auth"))
        except Exception as exc:  # noqa: BLE001 - the dashboard must still start
            self.auth_status = f"authentication failed: {exc}"
            self.last_error = repr(exc)
            self.error_count += 1
            self._publish_status()
            log.warning("live account authentication failed", extra={"error": repr(exc)})
            return False

        exchange = ClobExchange(self._auth.client, signature_type=settings.signature_type,
                                retry_policy=self._retry, logger=log, metrics=self._metrics)
        self._exchange = ReadOnlyExchange(exchange)
        self._data_api = RestClient(settings.data_api_host, timeout=self._s.http_timeout,
                                    retry_policy=self._retry, logger=log, metrics=self._metrics,
                                    name="data_api")
        self.auth_status = "authenticated"
        self.connected = True
        self.last_error = None
        self.state["wallet"].update({
            "signer": self._auth.address,
            "deposit_wallet": self._auth.funder,
            "address": self._auth.funder or self._auth.address,
            "signature_type": settings.signature_type,
            "creds_source": self._auth.creds_source,
            "builder_enabled": self._auth.builder_enabled,
            "min_allowance": settings.min_allowance,
        })
        log.info("live account sync authenticated", extra={"signer": self._auth.address,
                                                           "funder": self._auth.funder})
        self._user_ws = ClobWebSocket(
            settings.clob_ws_url, channel="user",
            subscribe_message=user_subscription(list(settings.markets), self._auth.api_key,
                                                self._auth.api_secret, self._auth.api_passphrase),
            on_message=self._on_user_event, logger=log, metrics=self._metrics,
            ping_interval=settings.ws_ping_interval, stale_timeout=settings.ws_stale_timeout,
            max_backoff=settings.ws_reconnect_max_backoff)
        return True

    async def run(self) -> None:
        """Poll loop. Wakes early whenever the user websocket reports activity."""
        # Authentication may not have succeeded at boot (CLOB unreachable, config
        # not filled in yet). Keep retrying with backoff rather than giving up.
        backoff = 15.0
        while self._exchange is None and not self._stop.is_set():
            if not self._s.live_sync_enabled:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                pass
            log.info("retrying live account authentication", extra={"after_s": backoff})
            await self.start()
            backoff = min(300.0, backoff * 2)

        ws_task = asyncio.create_task(self._user_ws.run()) if self._user_ws else None
        try:
            while not self._stop.is_set():
                await self.sync_once()
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           timeout=self._s.live_sync_interval_s)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass
        finally:
            if ws_task is not None:
                if self._user_ws:
                    self._user_ws.stop()
                ws_task.cancel()
                await asyncio.gather(ws_task, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._user_ws:
            self._user_ws.stop()

    async def aclose(self) -> None:
        self.stop()
        if self._data_api is not None:
            try:
                await self._data_api.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def _on_user_event(self, event: dict[str, Any]) -> None:
        """A fill or order update arrived — refresh now instead of on the timer."""
        etype = str(event.get("event_type") or event.get("type") or "").lower()
        if etype in ("order", "trade"):
            self.state["last_ws_event"] = {"type": etype, "ts": now_s(),
                                           "status": event.get("status")}
            self._wake.set()

    # ======================================================================
    # Sync
    # ======================================================================
    async def sync_once(self) -> None:
        """One refresh pass. Individual failures degrade that section only."""
        if self._exchange is None:
            return
        started = now_ms()
        slow = (now_s() - self.last_slow_sync_ts) >= self._s.live_slow_sync_interval_s

        results = await asyncio.gather(
            self._sync_health(),
            self._sync_balance(),
            self._sync_orders(),
            self._sync_trades(),
            self._sync_positions() if slow else _noop(),
            self._sync_value() if slow else _noop(),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        for failure in failures:
            self.error_count += 1
            self.last_error = repr(failure)
            log.debug("live sync section failed", extra={"error": repr(failure)})

        if slow:
            self.last_slow_sync_ts = now_s()
        self.last_sync_ts = now_s()
        self.sync_count += 1
        self._recompute_derived()
        self._publish_status(latency_ms=round(now_ms() - started, 1),
                             sections_failed=len(failures))

    def _publish_status(self, *, latency_ms: float | None = None,
                        sections_failed: int = 0) -> None:
        """Refresh the status block the UI renders.

        Called after every sync *and* on every authentication outcome, so a
        failure to connect is visible in the dashboard instead of the panel
        sitting silently on "not started".
        """
        self.state["status"] = {
            "api_ok": bool(self.state["status"].get("api_ok", False)),
            "authenticated": self.auth_status == "authenticated",
            "auth_status": self.auth_status,
            "enabled": self._s.live_sync_enabled,
            "websocket_connected": bool(self._user_ws and self._user_ws.connected),
            "websocket": self._user_ws.status if self._user_ws else None,
            "last_sync_ts": self.last_sync_ts,
            "last_sync_iso": utcnow_iso() if self.last_sync_ts else None,
            "sync_latency_ms": latency_ms,
            "sync_count": self.sync_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "clob_latency": self._metrics.latency_stats("clob.latency"),
            "sections_failed": sections_failed,
        }

    async def _sync_health(self) -> None:
        assert self._exchange
        async with self._clob_lock:
            ok = await self._exchange.health()
            self.state["status"]["api_ok"] = bool(ok)
            if ok:
                try:
                    self.state["status"]["server_time"] = await self._exchange.server_time()
                except Exception:  # noqa: BLE001 - informational only
                    pass

    async def _sync_balance(self) -> None:
        assert self._exchange
        async with self._clob_lock:
            balance = await self._exchange.get_collateral_balance()
            allowances = await self._exchange.get_collateral_allowances()
        finite = {k: v for k, v in allowances.items() if v not in (None,)}
        min_allowance = float(self.state["wallet"].get("min_allowance") or 0.0)
        self.state["wallet"].update({
            "balance": round(float(balance), 6),
            "allowances": {k: (None if v == float("inf") else round(v, 4)) for k, v in finite.items()},
            "allowance_min": min(finite.values()) if finite else 0.0,
            "allowance_ok": bool(finite) and min(finite.values()) >= min_allowance,
            "balance_ts": now_s(),
        })

    async def _sync_orders(self) -> None:
        assert self._exchange
        async with self._clob_lock:
            rows = await self._exchange.get_orders()
        orders = [_normalise_order(row) for row in rows or []]
        self.state["open_orders"] = orders

    async def _sync_trades(self) -> None:
        assert self._exchange
        async with self._clob_lock:
            rows = await self._exchange.get_trades()
        self.state["trade_history"] = [_normalise_trade(row) for row in (rows or [])][:500]

    async def _sync_positions(self) -> None:
        if self._data_api is None or not self._funder:
            return
        rows = await self._data_api.get("/positions", params={
            "user": self._funder, "sizeThreshold": 0.01, "limit": 500})
        self.state["positions"] = [_normalise_position(row) for row in (rows or [])]

    async def _sync_value(self) -> None:
        if self._data_api is None or not self._funder:
            return
        payload = await self._data_api.get("/value", params={"user": self._funder})
        value = None
        if isinstance(payload, list) and payload:
            value = _num(payload[0].get("value"))
        elif isinstance(payload, dict):
            value = _num(payload.get("value"))
        if value is not None:
            self.state["portfolio"]["positions_value_account"] = round(value, 6)

    @property
    def _funder(self) -> str:
        return str(self.state["wallet"].get("deposit_wallet") or "")

    # ======================================================================
    # Derived figures
    # ======================================================================
    def _recompute_derived(self) -> None:
        wallet = self.state["wallet"]
        orders = self.state["open_orders"]
        positions = self.state["positions"]

        # Collateral committed to resting buy orders is not spendable.
        locked = sum(o["remaining_size"] * o["price"] for o in orders if o["side"] == "BUY")
        balance = float(wallet.get("balance") or 0.0)
        wallet["locked"] = round(locked, 6)
        wallet["available"] = round(max(0.0, balance - locked), 6)
        allowance_min = wallet.get("allowance_min")
        # Buying power is capped by whichever binds first: cash or approval.
        wallet["buying_power"] = round(
            min(wallet["available"], allowance_min) if isinstance(allowance_min, (int, float))
            else wallet["available"], 6)

        positions_value = sum(p["current_value"] for p in positions)
        unrealized = sum(p["unrealized_pnl"] for p in positions)
        realized = sum(p["realized_pnl"] for p in positions)
        exposure = sum(p["initial_value"] for p in positions)
        self.state["portfolio"].update({
            "cash": round(balance, 6),
            "positions_value": round(positions_value, 6),
            "total_value": round(balance + positions_value, 6),
            "unrealized_pnl": round(unrealized, 6),
            "realized_pnl": round(realized, 6),
            "total_pnl": round(unrealized + realized, 6),
            "exposure": round(exposure, 6),
            "open_positions": len(positions),
            "open_orders": len(orders),
            "open_order_notional": round(
                sum(o["remaining_size"] * o["price"] for o in orders), 6),
        })
        self.state["fees_paid"] = round(
            sum(t.get("fee") or 0.0 for t in self.state["trade_history"]), 6)

    # ======================================================================
    def snapshot(self) -> dict[str, Any]:
        """The live-account block sent to the browser."""
        return self.state


# ======================================================================
# Normalisers — Polymarket returns several shapes across endpoints/versions
# ======================================================================
def _normalise_order(row: dict[str, Any]) -> dict[str, Any]:
    size = _num(row.get("original_size") or row.get("size")) or 0.0
    matched = _num(row.get("size_matched")) or 0.0
    price = _num(row.get("price")) or 0.0
    status = str(row.get("status") or "LIVE").lower()
    return {
        "order_id": str(row.get("id") or row.get("order_id") or ""),
        "created_ts": _num(row.get("created_at")) or 0.0,
        "market_id": str(row.get("market") or row.get("condition_id") or ""),
        "token_id": str(row.get("asset_id") or ""),
        "outcome": row.get("outcome"),
        "side": str(row.get("side") or "").upper(),
        "order_type": row.get("order_type") or row.get("type"),
        "price": price,
        "size": size,
        "filled_size": matched,
        "remaining_size": max(0.0, size - matched),
        "filled_pct": round(matched / size * 100.0, 2) if size else 0.0,
        "notional": round(price * size, 6),
        "status": status,
        "expiration": row.get("expiration"),
    }


def _normalise_trade(row: dict[str, Any]) -> dict[str, Any]:
    size = _num(row.get("size")) or 0.0
    price = _num(row.get("price")) or 0.0
    # Some responses carry an absolute fee, others only a rate in basis points.
    fee = _num(row.get("fee"))
    if fee is None:
        bps = _num(row.get("fee_rate_bps")) or 0.0
        fee = price * size * bps / 10_000.0
    return {
        "trade_id": str(row.get("id") or row.get("trade_id") or ""),
        "order_id": str(row.get("taker_order_id") or row.get("order_id") or ""),
        "ts": _num(row.get("match_time") or row.get("timestamp") or row.get("created_at")) or 0.0,
        "market_id": str(row.get("market") or row.get("condition_id") or ""),
        "token_id": str(row.get("asset_id") or ""),
        "outcome": row.get("outcome"),
        "side": str(row.get("side") or "").upper(),
        "price": price,
        "size": size,
        "notional": round(price * size, 6),
        "fee": round(fee, 6),
        "status": str(row.get("status") or ""),
        "maker": bool(row.get("maker_address")),
    }


def _normalise_position(row: dict[str, Any]) -> dict[str, Any]:
    size = _num(row.get("size")) or 0.0
    avg = _num(row.get("avgPrice") or row.get("avg_price")) or 0.0
    cur = _num(row.get("curPrice") or row.get("cur_price")) or 0.0
    initial = _num(row.get("initialValue"))
    current = _num(row.get("currentValue"))
    return {
        "token_id": str(row.get("asset") or row.get("token_id") or ""),
        "market_id": str(row.get("conditionId") or row.get("condition_id") or ""),
        "title": row.get("title") or row.get("slug") or "",
        "outcome": row.get("outcome") or "",
        "size": size,
        "avg_price": avg,
        "current_price": cur,
        "initial_value": initial if initial is not None else round(size * avg, 6),
        "current_value": current if current is not None else round(size * cur, 6),
        "unrealized_pnl": _num(row.get("cashPnl")) or round(size * (cur - avg), 6),
        "percent_pnl": _num(row.get("percentPnl")) or 0.0,
        "realized_pnl": _num(row.get("realizedPnl")) or 0.0,
        "redeemable": bool(row.get("redeemable")),
        "end_date": row.get("endDate"),
    }


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _noop() -> None:
    return None


def _empty_state() -> dict[str, Any]:
    return {
        "wallet": {
            "address": "", "signer": "", "deposit_wallet": "", "balance": 0.0,
            "available": 0.0, "locked": 0.0, "buying_power": 0.0, "allowances": {},
            "allowance_ok": False, "allowance_min": 0.0, "min_allowance": 0.0,
            "builder_enabled": False, "creds_source": None, "signature_type": None,
        },
        "open_orders": [],
        "trade_history": [],
        "positions": [],
        "portfolio": {
            "cash": 0.0, "positions_value": 0.0, "total_value": 0.0, "unrealized_pnl": 0.0,
            "realized_pnl": 0.0, "total_pnl": 0.0, "exposure": 0.0, "open_positions": 0,
            "open_orders": 0, "open_order_notional": 0.0,
        },
        "fees_paid": 0.0,
        "last_ws_event": None,
        "status": {"api_ok": False, "authenticated": False, "auth_status": "not started",
                   "websocket_connected": False, "last_sync_ts": 0.0, "sync_count": 0,
                   "error_count": 0, "last_error": None},
    }
