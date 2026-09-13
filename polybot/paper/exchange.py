"""Simulated exchange: real order books, imaginary money.

``PaperExchange`` presents the same async surface the engine uses against the real
CLOB, so :class:`~polybot.orders.manager.OrderManager` cannot tell the difference
and every live code path — submission, reconciliation, fills, cancels, recovery —
is exercised for real.

Market *reads* are delegated to a keyless public CLOB client, so books, tick sizes
and market metadata are genuine. Order *writes* never leave the process: they rest
in an internal book and are matched against the real depth that the websocket feed
and REST refresh loop push in, with configurable latency, partial-fill behaviour,
slippage and fees.
"""
from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from polybot.exchange.clob import ClobExchange
from polybot.orders.models import Action, OrderIntent
from polybot.utils.time_utils import now_s

_ids = itertools.count(1)


@dataclass
class PaperOrder:
    """One simulated resting order."""

    order_id: str
    market_id: str
    token_id: str
    strategy_side: str
    side: str                     # 'BUY' | 'SELL'
    price: float
    size: float
    created_ts: float
    matched: float = 0.0
    avg_price: float = 0.0
    fees: float = 0.0
    status: str = "live"
    reserved: float = 0.0
    fills: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.matched)

    @property
    def is_open(self) -> bool:
        return self.status in ("live", "partially_filled")

    def as_row(self) -> dict[str, Any]:
        """Shaped like a CLOB ``GET /orders`` row so the reconciler can read it."""
        return {
            "id": self.order_id,
            "order_id": self.order_id,
            "asset_id": self.token_id,
            "market": self.market_id,
            "side": self.side,
            "price": self.price,
            "original_size": self.size,
            "size": self.size,
            "size_matched": self.matched,
            "status": self.status,
            "fee": self.fees,
            "created_at": self.created_ts,
        }


class PaperExchange:
    """Async facade that matches orders internally against real book depth."""

    def __init__(self, reader: ClobExchange, account: Any, *, fee_bps: float = 0.0,
                 fill_latency_ms: float = 250.0, slippage_ticks: float = 0.0,
                 maker_fill_ratio: float = 1.0,
                 inventory_provider: Callable[[str, str], float] | None = None,
                 logger: logging.Logger | None = None, metrics: Any = None) -> None:
        self._reader = reader
        self._account = account
        self._fee_rate = max(0.0, fee_bps) / 10_000.0
        self._latency_s = max(0.0, fill_latency_ms) / 1000.0
        self._slippage_ticks = max(0.0, slippage_ticks)
        self._fill_ratio = min(1.0, max(0.01, maker_fill_ratio))
        self._inventory = inventory_provider
        self._log = logger or logging.getLogger("polybot.paper.exchange")
        self._metrics = metrics

        self._lock = threading.RLock()
        self._orders: dict[str, PaperOrder] = {}
        self._books: dict[str, dict[str, list[tuple[float, float]]]] = {}
        self._ticks: dict[str, float] = {}
        self._trades: list[dict[str, Any]] = []

    # ======================================================================
    # Market data — delegated to the real (public) CLOB
    # ======================================================================
    async def health(self) -> bool:
        return await self._reader.health()

    async def server_time(self) -> Any:
        return await self._reader.server_time()

    def address(self) -> str:
        return "paper"

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        return await self._reader.get_market(condition_id)

    async def get_order_book(self, token_id: str) -> Any:
        return await self._reader.get_order_book(token_id)

    async def get_tick_size(self, token_id: str) -> float:
        tick = await self._reader.get_tick_size(token_id)
        self._ticks[token_id] = tick
        return tick

    async def get_neg_risk(self, token_id: str) -> bool:
        return await self._reader.get_neg_risk(token_id)

    async def best_bid_ask(self, token_id: str) -> dict[str, float]:
        quote = await self._reader.best_bid_ask(token_id)
        self._ticks[token_id] = quote.get("tick_size", 0.01) or 0.01
        return quote

    async def refresh_depth(self, token_id: str) -> None:
        """Pull a full REST book snapshot into the matching engine.

        get_order_book returns a plain dict on py-clob-client-v2 (bids/asks are
        lists of {"price": ..., "size": ...} dicts), not an object with
        attributes -- index it accordingly.
        """
        book = await self._reader.get_order_book(token_id)
        self.apply_book(token_id,
                        bids=book.get("bids") or [],
                        asks=book.get("asks") or [])

    def apply_book(self, token_id: str, *, bids: Iterable[Any], asks: Iterable[Any]) -> None:
        """Store normalised depth for a token (from websocket or REST)."""
        parsed_bids = sorted(_levels(bids), key=lambda lvl: -lvl[0])
        parsed_asks = sorted(_levels(asks), key=lambda lvl: lvl[0])
        with self._lock:
            self._books[token_id] = {"bids": parsed_bids, "asks": parsed_asks}

    # ======================================================================
    # Order entry
    # ======================================================================
    async def place_order(self, intent: OrderIntent, *, tick_size: float | None = None,
                          neg_risk: bool = False) -> dict[str, Any]:
        """Accept an order into the simulated book (or reject it, as a venue would)."""
        notional = intent.price * intent.size
        if tick_size:
            self._ticks[intent.token_id] = tick_size

        if intent.action == Action.BUY:
            if not self._account.reserve(notional):
                return {"success": False,
                        "errorMsg": f"insufficient paper collateral: need {notional:.4f}, "
                                    f"available {self._account.available:.4f}"}
        elif self._inventory is not None:
            held = self._inventory(intent.market_id, intent.strategy_side)
            resting = self._resting_sell_size(intent.token_id)
            if intent.size > held - resting + 1e-9:
                return {"success": False,
                        "errorMsg": f"insufficient paper shares: need {intent.size:g}, "
                                    f"held {held:g} ({resting:g} already offered)"}

        order = PaperOrder(
            order_id=f"paper-{next(_ids):09d}",
            market_id=intent.market_id,
            token_id=intent.token_id,
            strategy_side=intent.strategy_side,
            side=intent.action.value,
            price=intent.price,
            size=intent.size,
            created_ts=now_s(),
            reserved=notional if intent.action == Action.BUY else 0.0,
        )
        with self._lock:
            self._orders[order.order_id] = order
        self._log.info("paper order accepted", extra={"order_id": order.order_id,
                       "market": intent.market_id, "side": intent.strategy_side,
                       "action": order.side, "price": order.price, "size": order.size})
        if self._metrics:
            self._metrics.incr("paper.orders.accepted")
        return {"success": True, "orderID": order.order_id, "status": "live", "paper": True}

    async def cancel(self, order_id: str) -> Any:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or not order.is_open:
                return {"success": False, "errorMsg": "order not open"}
            self._close(order, "canceled")
        return {"success": True, "canceled": [order_id]}

    async def cancel_orders(self, order_ids: list[str]) -> Any:
        for order_id in order_ids:
            await self.cancel(order_id)
        return {"success": True, "canceled": order_ids}

    async def cancel_all(self) -> Any:
        with self._lock:
            targets = [o for o in self._orders.values() if o.is_open]
            for order in targets:
                self._close(order, "canceled")
        return {"success": True, "canceled": [o.order_id for o in targets]}

    async def cancel_market_orders(self, *, market: str = "", asset_id: str = "") -> Any:
        with self._lock:
            targets = [o for o in self._orders.values() if o.is_open
                       and (not market or o.market_id == market)
                       and (not asset_id or o.token_id == asset_id)]
            for order in targets:
                self._close(order, "canceled")
        return {"success": True, "canceled": [o.order_id for o in targets]}

    def _close(self, order: PaperOrder, status: str) -> None:
        """Terminate an order and hand back any collateral it was reserving."""
        if order.reserved > 0:
            self._account.release(order.reserved)
            order.reserved = 0.0
        order.status = status

    # ======================================================================
    # Reads used by the reconciler
    # ======================================================================
    async def get_orders(self, *, market: str | None = None,
                         asset_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [o.as_row() for o in self._orders.values() if o.is_open
                    and (market is None or o.market_id == market)
                    and (asset_id is None or o.token_id == asset_id)]

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            order = self._orders.get(order_id)
            return order.as_row() if order else None

    async def get_trades(self, *, market: str | None = None,
                         asset_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [t for t in self._trades
                    if (market is None or t["market"] == market)
                    and (asset_id is None or t["asset_id"] == asset_id)]

    async def get_collateral_balance(self) -> float:
        return self._account.cash

    async def get_collateral_allowances(self) -> dict[str, float]:
        # A simulated account has unlimited approval; nothing is ever on-chain.
        return {"paper": float("inf")}

    async def refresh_collateral_allowance(self) -> Any:
        return {"success": True, "paper": True}

    # ======================================================================
    # Matching
    # ======================================================================
    def match(self) -> list[dict[str, Any]]:
        """Run one matching pass. Returns the fills produced.

        Liquidity consumed by one order in a pass is not offered to the next, so
        several resting orders competing for the same level fill in sequence
        rather than all at once.
        """
        cutoff = now_s() - self._latency_s
        fills: list[dict[str, Any]] = []
        with self._lock:
            working = {token: {"bids": list(book["bids"]), "asks": list(book["asks"])}
                       for token, book in self._books.items()}
            candidates = [o for o in self._orders.values()
                          if o.is_open and o.created_ts <= cutoff]
            candidates.sort(key=lambda o: o.created_ts)
            for order in candidates:
                book = working.get(order.token_id)
                if not book:
                    continue
                fill = self._match_one(order, book)
                if fill is not None:
                    fills.append(fill)
        return fills

    def _match_one(self, order: PaperOrder, book: dict[str, list[tuple[float, float]]]
                   ) -> dict[str, Any] | None:
        tick = self._ticks.get(order.token_id, 0.01) or 0.01
        slip = self._slippage_ticks * tick
        if order.side == "BUY":
            levels, key = book["asks"], "asks"
            eligible = [(p, s) for p, s in levels if p <= order.price + 1e-9]
        else:
            levels, key = book["bids"], "bids"
            eligible = [(p, s) for p, s in levels if p >= order.price - 1e-9]
        if not eligible:
            return None

        want = order.remaining * self._fill_ratio
        want = min(order.remaining, max(want, 0.0))
        if want <= 0:
            return None

        filled = 0.0
        cost = 0.0
        consumed: list[tuple[float, float]] = []
        for price, size in eligible:
            if filled >= want - 1e-9:
                break
            take = min(size, want - filled)
            exec_price = price + slip if order.side == "BUY" else price - slip
            exec_price = min(1.0, max(0.0, exec_price))
            filled += take
            cost += take * exec_price
            consumed.append((price, take))
        if filled <= 1e-9:
            return None

        # Remove the liquidity this order just took from the working book.
        remaining_levels = []
        taken = dict(consumed)
        for price, size in levels:
            left = size - taken.get(price, 0.0)
            if left > 1e-9:
                remaining_levels.append((price, left))
        book[key] = remaining_levels

        avg_price = cost / filled
        fee = cost * self._fee_rate
        self._settle(order, filled, avg_price, cost, fee)
        fill = {"order_id": order.order_id, "market": order.market_id,
                "asset_id": order.token_id, "side": order.side, "price": avg_price,
                "size": filled, "fee": fee, "ts": now_s()}
        order.fills.append(fill)
        self._trades.append({**fill, "id": f"papertrade-{len(self._trades) + 1:09d}"})
        if self._metrics:
            self._metrics.incr("paper.fills")
        self._log.info("paper fill", extra={"order_id": order.order_id, "price": round(avg_price, 4),
                                            "size": filled, "fee": round(fee, 6)})
        return fill

    def _settle(self, order: PaperOrder, filled: float, avg_price: float, cost: float,
                fee: float) -> None:
        """Move virtual cash and advance the order's lifecycle."""
        prior = order.matched
        order.matched = prior + filled
        order.avg_price = ((order.avg_price * prior) + avg_price * filled) / order.matched
        order.fees += fee
        if order.side == "BUY":
            release = min(order.reserved, order.price * filled)
            self._account.release(release)
            order.reserved -= release
            self._account.debit(cost, fee=fee)
        else:
            self._account.credit(cost, fee=fee)
        if order.remaining <= 1e-9:
            order.status = "filled"
            if order.reserved > 0:
                # Filled below the limit price: hand back the unspent reservation.
                self._account.release(order.reserved)
                order.reserved = 0.0
        else:
            order.status = "partially_filled"

    # ======================================================================
    # Introspection
    # ======================================================================
    def _resting_sell_size(self, token_id: str) -> float:
        return sum(o.remaining for o in self._orders.values()
                   if o.is_open and o.side == "SELL" and o.token_id == token_id)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            orders = list(self._orders.values())
        return {
            "open_orders": sum(1 for o in orders if o.is_open),
            "total_orders": len(orders),
            "fills": sum(len(o.fills) for o in orders),
            "books_tracked": len(self._books),
            "account": self._account.snapshot(),
        }


def _levels(raw: Iterable[Any]) -> list[tuple[float, float]]:
    """Normalise book levels from dicts, objects or ``[price, size]`` pairs."""
    out: list[tuple[float, float]] = []
    for level in raw or ():
        try:
            if isinstance(level, dict):
                price, size = float(level.get("price", 0)), float(level.get("size", 0))
            elif isinstance(level, (list, tuple)):
                price, size = float(level[0]), float(level[1])
            else:
                price, size = float(level.price), float(level.size)
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if price > 0 and size > 0:
            out.append((price, size))
    return out
