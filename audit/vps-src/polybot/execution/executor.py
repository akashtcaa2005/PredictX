"""The Broker adapter — the synchronous execution surface the strategy drives.

The compiled strategy calls ``self.broker.place_limit / sell / position_qty /
cancel_all_open / notional_in_market`` from inside :meth:`on_bar`. Those calls run
in the strategy worker thread and must be fast and non-blocking, so this adapter
only does in-memory work: it resolves the outcome token, snaps the price/size to
the market's constraints, and hands the intent to the :class:`OrderManager`, which
performs the actual (async, retryable) network I/O on the event loop.
"""
from __future__ import annotations

import logging
from typing import Any

from polybot.exchange.market_registry import MarketRegistry
from polybot.orders.manager import OrderManager
from polybot.orders.models import Action, OrderIntent


class Broker:
    """Implements the ``base.Broker`` protocol the strategy binds to."""

    def __init__(self, order_manager: OrderManager, registry: MarketRegistry, portfolio: Any,
                 *, default_order_type: str = "GTC", logger: logging.Logger | None = None,
                 metrics: Any = None, on_signal: Any = None) -> None:
        self._orders = order_manager
        self._registry = registry
        self._portfolio = portfolio
        self._order_type = default_order_type
        self._log = logger or logging.getLogger("polybot.execution")
        self._metrics = metrics
        # Optional ``(market_id, signal) -> None`` hook; the telemetry publisher
        # uses it to surface what the strategy asked for on each tick.
        self._on_signal = on_signal

    # -- order entry -----------------------------------------------------------
    def place_limit(self, market_id: str, side: str, price: float, qty: float) -> Any:
        return self._enter(market_id, side, Action.BUY, price, qty)

    def sell(self, market_id: str, side: str, price: float, size: float) -> Any:
        return self._enter(market_id, side, Action.SELL, price, size)

    def _enter(self, market_id: str, side: str, action: Action, price: float, size: float) -> Any:
        intent = self._build_intent(market_id, side, action, price, size)
        if intent is None:
            return None
        if self._metrics:
            self._metrics.incr(f"strategy.{action.value.lower()}")
        self._signal(market_id, f"{action.value} {side}")
        return self._orders.submit_intent(intent)

    def _signal(self, market_id: str, signal: str) -> None:
        if self._on_signal is None:
            return
        try:
            self._on_signal(market_id, signal)
        except Exception:  # noqa: BLE001 - a telemetry hook must never break execution
            pass

    def _build_intent(self, market_id: str, side: str, action: Action,
                      price: float, size: float) -> OrderIntent | None:
        state = self._registry.state(market_id)
        info = self._registry.info(market_id)
        if state is None or info is None:
            self._log.warning("unknown market; order skipped", extra={"market": market_id, "side": side})
            return None
        if side not in ("up", "down"):
            self._log.warning("unknown side; order skipped", extra={"market": market_id, "side": side})
            return None

        token_id = state.token_id(side)
        snapped_price = _snap_price(price, info.tick_size)
        snapped_size = _snap_size(size, info.min_order_size)
        if snapped_price <= 0 or snapped_size <= 0:
            self._log.warning("degenerate order skipped", extra={"market": market_id, "side": side,
                             "price": price, "size": size})
            return None

        return OrderIntent(
            market_id=market_id, strategy_side=side, token_id=token_id, action=action,
            price=snapped_price, size=snapped_size, order_type=self._order_type,
        )

    # -- reads the strategy uses ----------------------------------------------
    def position_qty(self, market_id: str, side: str) -> float:
        return self._portfolio.position_qty(market_id, side)

    def cancel_all_open(self, market_id: str) -> Any:
        if self._metrics:
            self._metrics.incr("strategy.cancel_all")
        self._signal(market_id, "CANCEL")
        return self._orders.enqueue_cancel_market(market_id)

    def notional_in_market(self, market_id: str) -> float:
        return self._orders.market_open_notional(market_id) + self._portfolio.position_notional(market_id)


def _snap_price(price: float, tick: float) -> float:
    """Round a price to the market's tick grid, clamped to (0, 1)."""
    tick = tick if tick and tick > 0 else 0.01
    snapped = round(round(price / tick) * tick, 6)
    return min(max(snapped, tick), 1.0 - tick)


def _snap_size(size: float, min_size: float) -> float:
    """Floor a size to whole shares and enforce the market minimum."""
    floored = float(int(size))
    if min_size and floored < min_size:
        return 0.0  # below the exchange minimum -> reject rather than upsize
    return floored
