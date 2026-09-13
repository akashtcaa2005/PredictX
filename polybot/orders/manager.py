"""Order manager — the bridge between strategy intents and the exchange.

Responsibilities:

* accept order **intents** synchronously from the strategy worker thread, run the
  risk gate, dedup, and record them with optimistic exposure accounting (so the
  strategy sees committed notional grow *within* a tick);
* submit / cancel / replace orders asynchronously on the event loop (retryable,
  never double-submitting);
* reconcile order state from the websocket ``user`` feed (real-time) and a REST
  poll (backstop), applying fills to the portfolio;
* recover pre-existing live orders on startup.

Order posting is skipped in dry-run mode (orders are marked ``simulated``) so the
whole pipeline can be exercised without touching funds.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from polybot.exchange.clob import ClobExchange
from polybot.exchange.market_registry import MarketRegistry
from polybot.orders.models import Action, Order, OrderIntent, OrderStatus
from polybot.telemetry.recorder import NullRecorder
from polybot.utils.time_utils import now_ms, now_s

_STATUS_MAP = {
    "live": OrderStatus.LIVE,
    "open": OrderStatus.LIVE,
    "delayed": OrderStatus.LIVE,
    "unmatched": OrderStatus.LIVE,
    "matched": OrderStatus.MATCHED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "partial": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "complete": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def _map_status(raw: Any, default: OrderStatus = OrderStatus.LIVE) -> OrderStatus:
    return _STATUS_MAP.get(str(raw or "").strip().lower(), default)


class OrderManager:
    """Tracks orders and mediates all exchange order operations."""

    def __init__(self, exchange: ClobExchange, registry: MarketRegistry, risk: Any, portfolio: Any,
                 *, dry_run: bool, dedup_window_s: float = 5.0,
                 logger: logging.Logger | None = None, trade_logger: logging.Logger | None = None,
                 metrics: Any = None, recorder: Any = None) -> None:
        self._exchange = exchange
        self._registry = registry
        self._risk = risk
        self._portfolio = portfolio
        self._dry_run = dry_run
        self._dedup_window = dedup_window_s
        self._log = logger or logging.getLogger("polybot.orders")
        self._trade_log = trade_logger or logging.getLogger("polybot.trade")
        self._metrics = metrics
        self._recorder = recorder or NullRecorder()

        self._lock = threading.RLock()
        self._orders: dict[str, Order] = {}
        self._by_exchange_id: dict[str, Order] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._stop = asyncio.Event()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the event loop so the worker thread can schedule work onto it."""
        self._loop = loop

    # ======================================================================
    # Synchronous surface (called from the strategy worker thread via Broker)
    # ======================================================================
    def submit_intent(self, intent: OrderIntent) -> Order | None:
        """Risk-check, dedup and enqueue an order. Returns the tracked Order or None."""
        with self._lock:
            dup = self._find_duplicate(intent)
            if dup is not None:
                self._log.info("duplicate order suppressed", extra={"market": intent.market_id,
                              "side": intent.strategy_side, "price": intent.price,
                              "size": intent.size, "existing": dup.client_id})
                if self._metrics:
                    self._metrics.incr("orders.deduped")
                return dup

            decision = self._risk.check_order(intent)
            if not decision.ok:
                if self._metrics:
                    self._metrics.incr("orders.risk_rejected")
                return None

            order = Order(intent=intent)
            self._orders[order.client_id] = order
            self._risk.record_submission(intent)

        if self._metrics:
            self._metrics.incr("orders.accepted")
        self._recorder.record_order(order)
        self._schedule(("submit", order))
        return order

    def enqueue_cancel_market(self, market_id: str) -> None:
        """Cancel every open order in a market (strategy ``cancel_all_open``)."""
        self._schedule(("cancel_market", market_id))

    def _schedule(self, command: tuple[str, Any]) -> None:
        loop = self._loop
        if loop is None:  # pragma: no cover - defensive; loop is bound at startup
            raise RuntimeError("OrderManager loop not bound; call bind_loop() first")
        loop.call_soon_threadsafe(self._queue.put_nowait, command)

    def _find_duplicate(self, intent: OrderIntent) -> Order | None:
        key = intent.dedup_key()
        cutoff = now_s() - self._dedup_window
        for order in self._orders.values():
            if order.status.is_open and order.intent.dedup_key() == key and order.created_ts >= cutoff:
                return order
        return None

    # ======================================================================
    # Accounting reads (thread-safe)
    # ======================================================================
    def market_open_notional(self, market_id: str) -> float:
        with self._lock:
            return sum(o.open_notional for o in self._orders.values()
                       if o.intent.market_id == market_id)

    def open_notional_total(self) -> float:
        with self._lock:
            return sum(o.open_notional for o in self._orders.values())

    def open_count(self) -> int:
        with self._lock:
            return sum(1 for o in self._orders.values() if o.status.is_open)

    def active_markets(self) -> set[str]:
        with self._lock:
            return {o.intent.market_id for o in self._orders.values() if o.status.is_open}

    def open_orders(self) -> list[Order]:
        with self._lock:
            return [o for o in self._orders.values() if o.status.is_open]

    # ======================================================================
    # Async execution loop
    # ======================================================================
    async def run(self) -> None:
        """Consume the command queue until stopped."""
        self._log.info("order manager started", extra={"dry_run": self._dry_run})
        while not self._stop.is_set():
            try:
                kind, payload = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                if kind == "submit":
                    await self._submit(payload)
                elif kind == "cancel_market":
                    await self._cancel_market(payload)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                self._log.error("order command failed", extra={"kind": kind, "error": repr(exc)},
                                exc_info=True)

    async def _submit(self, order: Order) -> None:
        intent = order.intent
        if self._dry_run:
            order.simulated = True
            order.status = OrderStatus.LIVE
            order.submitted_ts = now_s()
            order.touch()
            self._log.info("SIMULATED order (dry-run)", extra=_order_extra(order))
            if self._metrics:
                self._metrics.incr("orders.simulated")
            self._recorder.record_order(order)
            return

        info = self._registry.info(intent.market_id)
        tick = info.tick_size if info else None
        neg_risk = info.neg_risk if info else False
        started_ms = now_ms()
        try:
            resp = await self._exchange.place_order(intent, tick_size=tick, neg_risk=neg_risk)
        except Exception as exc:  # noqa: BLE001
            order.status = OrderStatus.FAILED
            order.last_error = repr(exc)
            order.touch()
            self._log.error("order submission failed", extra={**_order_extra(order), "error": repr(exc)})
            if self._metrics:
                self._metrics.incr("orders.failed")
            self._recorder.record_order(order, execution_ms=now_ms() - started_ms)
            self._recorder.record_event("critical", "order_failed",
                                        f"order submission failed in {intent.market_id}",
                                        error=repr(exc), client_id=order.client_id)
            return
        execution_ms = now_ms() - started_ms

        error_msg = resp.get("errorMsg") or resp.get("error")
        success = resp.get("success", not error_msg) and not error_msg
        if success:
            oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
            order.exchange_order_id = str(oid) if oid else None
            order.status = _map_status(resp.get("status"), OrderStatus.LIVE)
            order.submitted_ts = now_s()
            order.touch()
            with self._lock:
                if order.exchange_order_id:
                    self._by_exchange_id[order.exchange_order_id] = order
            self._log.info("order placed", extra={**_order_extra(order), "status": order.status.value})
            if self._metrics:
                self._metrics.incr("orders.placed")
        else:
            order.status = OrderStatus.REJECTED
            order.last_error = str(error_msg)
            order.touch()
            self._log.warning("order rejected by exchange", extra={**_order_extra(order),
                                                                   "error": order.last_error})
            if self._metrics:
                self._metrics.incr("orders.rejected")
            self._recorder.record_event("warning", "order_rejected",
                                        f"order rejected in {intent.market_id}: {order.last_error}",
                                        client_id=order.client_id)
        self._recorder.record_order(order, exchange_response=resp, execution_ms=execution_ms)

    async def _cancel_market(self, market_id: str) -> None:
        targets = [o for o in self.open_orders() if o.intent.market_id == market_id]
        if not targets:
            return
        if not self._dry_run:
            try:
                await self._exchange.cancel_market_orders(market=market_id)
            except Exception as exc:  # noqa: BLE001
                self._log.error("cancel_market failed", extra={"market": market_id, "error": repr(exc)})
                return
        for order in targets:
            order.status = OrderStatus.CANCELED
            order.touch()
            self._recorder.record_order(order)
        self._log.info("cancelled market orders", extra={"market": market_id, "count": len(targets),
                                                         "dry_run": self._dry_run})
        if self._metrics:
            self._metrics.incr("orders.canceled", len(targets))

    async def cancel_order(self, order: Order) -> None:
        """Cancel a single order by exchange id."""
        if not self._dry_run and order.exchange_order_id:
            try:
                await self._exchange.cancel(order.exchange_order_id)
            except Exception as exc:  # noqa: BLE001
                self._log.error("cancel failed", extra={**_order_extra(order), "error": repr(exc)})
                return
        order.status = OrderStatus.CANCELED
        order.touch()
        self._recorder.record_order(order)
        self._log.info("order canceled", extra=_order_extra(order))

    async def replace_order(self, order: Order, *, new_price: float, new_size: float) -> Order | None:
        """Order replacement = cancel the old order and submit a fresh one."""
        await self.cancel_order(order)
        replacement = OrderIntent(
            market_id=order.intent.market_id, strategy_side=order.intent.strategy_side,
            token_id=order.intent.token_id, action=order.intent.action,
            price=new_price, size=new_size, order_type=order.intent.order_type,
        )
        return self.submit_intent(replacement)

    # ======================================================================
    # Reconciliation
    # ======================================================================
    async def poll(self) -> None:
        """REST backstop: reconcile open orders and detect fills/terminal states."""
        if self._dry_run:
            return
        tracked = {o.exchange_order_id: o for o in self.open_orders() if o.exchange_order_id}
        if not tracked:
            return
        try:
            live = await self._exchange.get_orders()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("order poll failed", extra={"error": repr(exc)})
            return
        seen: set[str] = set()
        for row in live:
            oid = str(row.get("id") or row.get("order_id") or "")
            seen.add(oid)
            order = tracked.get(oid)
            if order is None:
                continue
            matched = float(row.get("size_matched") or 0.0)
            price = float(row.get("price") or order.intent.price)
            self._apply_matched(order, matched, price, row.get("status"), _fee(row))
        # Orders we thought were open but the exchange no longer lists: resolve them.
        for oid, order in tracked.items():
            if oid not in seen and order.status.is_open:
                await self._resolve_missing(order)

    async def _resolve_missing(self, order: Order) -> None:
        try:
            row = await self._exchange.get_order(order.exchange_order_id or "")
        except Exception:  # noqa: BLE001
            row = None
        if row:
            matched = float(row.get("size_matched") or 0.0)
            price = float(row.get("price") or order.intent.price)
            self._apply_matched(order, matched, price, row.get("status"), _fee(row))
        else:
            # Fully gone from the book and unqueryable: treat any residual as filled.
            if order.filled_size < order.intent.size:
                self._apply_fill(order, order.intent.price, order.intent.size - order.filled_size)
                order.filled_size = order.intent.size
            order.status = OrderStatus.FILLED
            order.touch()
            self._recorder.record_order(order)

    async def on_user_event(self, event: dict[str, Any]) -> None:
        """Handle a websocket ``user``-channel event (order or trade update)."""
        etype = str(event.get("event_type") or event.get("type") or "").lower()
        if etype == "order":
            oid = str(event.get("id") or event.get("order_id") or "")
            with self._lock:
                order = self._by_exchange_id.get(oid)
            if order is None:
                return
            matched = float(event.get("size_matched") or 0.0)
            price = float(event.get("price") or order.intent.price)
            self._apply_matched(order, matched, price, event.get("status"), _fee(event))
        elif etype == "trade":
            self._trade_log.info("trade event", extra={"asset_id": event.get("asset_id"),
                                 "price": event.get("price"), "size": event.get("size"),
                                 "side": event.get("side"), "status": event.get("status")})

    def _apply_matched(self, order: Order, cumulative_matched: float, price: float, raw_status: Any,
                       fee: float = 0.0) -> None:
        delta = cumulative_matched - order.filled_size
        if delta > 1e-9:
            self._apply_fill(order, price, delta, fee=fee)
            order.filled_size = cumulative_matched
        status = _map_status(raw_status, order.status)
        if order.filled_size >= order.intent.size - 1e-9:
            status = OrderStatus.FILLED
        elif order.filled_size > 0 and status.is_open:
            status = OrderStatus.PARTIALLY_FILLED
        order.status = status
        order.touch()
        self._recorder.record_order(order)

    def _apply_fill(self, order: Order, price: float, size: float, fee: float = 0.0) -> None:
        realized = self._portfolio.apply_fill(order.intent.token_id, order.intent.action, price, size)
        self._risk.record_fill(realized)
        # Running average execution price over all fills of this order.
        prior = order.filled_size
        order.avg_fill_price = ((order.avg_fill_price * prior) + price * size) / (prior + size)
        order.fees += fee
        self._trade_log.info("FILL", extra={**_order_extra(order), "fill_price": price,
                             "fill_size": size, "realized_pnl": round(realized, 6)})
        if self._metrics:
            self._metrics.incr("fills")
            self._metrics.incr("fills.size", size)
        self._recorder.record_fill(order, price=price, size=size, realized_pnl=realized, fee=fee)
        self._recorder.record_event(
            "info", "fill", f"{order.intent.action.value} {size:g} @ {price:g} in {order.intent.market_id}",
            client_id=order.client_id, realized_pnl=round(realized, 6))

    # ======================================================================
    # Startup recovery / shutdown
    # ======================================================================
    async def recover(self) -> int:
        """Adopt any pre-existing live orders on the exchange for our markets."""
        if self._dry_run:
            return 0
        try:
            live = await self._exchange.get_orders()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("order recovery failed", extra={"error": repr(exc)})
            return 0
        recovered = 0
        for row in live:
            token = str(row.get("asset_id") or "")
            resolved = self._registry.resolve_token(token)
            if resolved is None:
                continue
            market_id, side = resolved
            oid = str(row.get("id") or "")
            with self._lock:
                if oid and oid in self._by_exchange_id:
                    continue
            action = Action.BUY if str(row.get("side", "BUY")).upper() == "BUY" else Action.SELL
            intent = OrderIntent(market_id=market_id, strategy_side=side, token_id=token,
                                 action=action, price=float(row.get("price") or 0.0),
                                 size=float(row.get("original_size") or row.get("size") or 0.0))
            order = Order(intent=intent, exchange_order_id=oid,
                          status=_map_status(row.get("status"), OrderStatus.LIVE),
                          filled_size=float(row.get("size_matched") or 0.0))
            with self._lock:
                self._orders[order.client_id] = order
                if oid:
                    self._by_exchange_id[oid] = order
            self._recorder.record_order(order)
            recovered += 1
        self._log.info("recovered live orders", extra={"count": recovered})
        return recovered

    async def cancel_all(self) -> None:
        """Cancel every open order (used on graceful shutdown / kill switch)."""
        if self._dry_run:
            for order in self.open_orders():
                order.status = OrderStatus.CANCELED
                order.touch()
                self._recorder.record_order(order)
            return
        try:
            await self._exchange.cancel_all()
        except Exception as exc:  # noqa: BLE001
            self._log.error("cancel_all failed", extra={"error": repr(exc)})
            return
        for order in self.open_orders():
            order.status = OrderStatus.CANCELED
            order.touch()
            self._recorder.record_order(order)
        self._log.info("cancelled all open orders")
        self._recorder.record_event("warning", "cancel_all", "all open orders cancelled")

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            orders = list(self._orders.values())
        by_status: dict[str, int] = {}
        for o in orders:
            by_status[o.status.value] = by_status.get(o.status.value, 0) + 1
        return {
            "total": len(orders),
            "open": sum(1 for o in orders if o.status.is_open),
            "open_notional": round(self.open_notional_total(), 4),
            "by_status": by_status,
        }


def _fee(row: dict[str, Any]) -> float:
    """Fee charged on a fill, in collateral units. 0.0 when the venue reports none."""
    for key in ("fee", "fee_paid", "feeAmount", "maker_fee", "taker_fee"):
        raw = row.get(key)
        if raw not in (None, ""):
            try:
                return abs(float(raw))
            except (TypeError, ValueError):
                continue
    return 0.0


def _order_extra(order: Order) -> dict[str, Any]:
    i = order.intent
    return {"client_id": order.client_id, "exchange_id": order.exchange_order_id,
            "market": i.market_id, "side": i.strategy_side, "action": i.action.value,
            "price": i.price, "size": i.size, "type": i.order_type}
