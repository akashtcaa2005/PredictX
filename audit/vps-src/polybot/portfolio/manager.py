"""Portfolio & position management: positions, exposure and PnL.

Positions are seeded once from the public data-API (keyed by the funder/proxy
address) and thereafter updated from fills (websocket ``user`` channel, with the
order-poll reconciler as a backstop). All reads/writes are guarded by a lock
because the strategy worker thread reads ``position_qty`` while the event loop
applies fills.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from polybot.api.rest_client import RestClient
from polybot.exchange.market_registry import MarketRegistry
from polybot.orders.models import Action


@dataclass
class Position:
    """A single (market, side) position with cost basis and realized PnL."""

    market_id: str
    side: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def unrealized(self, mark_price: float) -> float:
        return (mark_price - self.avg_price) * self.qty


class PortfolioManager:
    """Owns positions and derives exposure + PnL."""

    def __init__(self, registry: MarketRegistry, *, funder_address: str,
                 data_api: RestClient | None = None, logger: logging.Logger | None = None) -> None:
        self._registry = registry
        self._funder = funder_address
        self._data_api = data_api
        self._log = logger or logging.getLogger("polybot.portfolio")
        self._lock = threading.Lock()
        self._positions: dict[tuple[str, str], Position] = {}

    # -- seeding ---------------------------------------------------------------
    async def seed_from_data_api(self) -> int:
        """Load existing positions from the data-API. Best-effort; never fatal."""
        if self._data_api is None or not self._funder:
            return 0
        try:
            rows = await self._data_api.get("/positions", params={"user": self._funder})
        except Exception as exc:  # noqa: BLE001 - seeding is optional
            self._log.warning("could not seed positions from data-api", extra={"error": repr(exc)})
            return 0
        count = 0
        for row in rows or []:
            token_id = str(row.get("asset") or row.get("token_id") or "")
            resolved = self._registry.resolve_token(token_id)
            if resolved is None:
                continue
            market_id, side = resolved
            qty = float(row.get("size") or row.get("shares") or 0.0)
            avg = float(row.get("avgPrice") or row.get("avg_price") or 0.0)
            if qty <= 0:
                continue
            with self._lock:
                self._positions[(market_id, side)] = Position(market_id, side, qty, avg)
            count += 1
        self._log.info("seeded positions", extra={"count": count})
        return count

    def restore(self, rows: list[dict[str, Any]]) -> int:
        """Seed positions from persisted telemetry rows (used by the paper engine,
        which has no external account to read positions back from)."""
        count = 0
        with self._lock:
            for row in rows:
                qty = float(row.get("qty") or 0.0)
                if qty <= 0:
                    continue
                market_id = str(row.get("market_id") or "")
                side = str(row.get("side") or "")
                if not market_id or side not in ("up", "down"):
                    continue
                self._positions[(market_id, side)] = Position(
                    market_id, side, qty, float(row.get("avg_price") or 0.0),
                    float(row.get("realized_pnl") or 0.0))
                count += 1
        self._log.info("restored positions from telemetry", extra={"count": count})
        return count

    # -- reads (thread-safe; called from the strategy worker thread) -----------
    def position_qty(self, market_id: str, side: str) -> float:
        with self._lock:
            pos = self._positions.get((market_id, side))
            return pos.qty if pos else 0.0

    def position_notional(self, market_id: str) -> float:
        """Cost-basis notional of open positions across both sides of a market."""
        with self._lock:
            return sum(p.qty * p.avg_price for (m, _), p in self._positions.items() if m == market_id)

    def notional_in_market(self, market_id: str) -> float:
        return self.position_notional(market_id)

    def total_exposure(self) -> float:
        with self._lock:
            return sum(p.qty * p.avg_price for p in self._positions.values())

    def active_markets(self) -> set[str]:
        with self._lock:
            return {m for (m, _), p in self._positions.items() if p.qty > 0}

    # -- fills -----------------------------------------------------------------
    def apply_fill(self, token_id: str, action: Action, price: float, size: float) -> float:
        """Apply a fill; returns realized PnL delta (non-zero only on sells)."""
        resolved = self._registry.resolve_token(token_id)
        if resolved is None or size <= 0:
            return 0.0
        market_id, side = resolved
        realized = 0.0
        with self._lock:
            pos = self._positions.setdefault((market_id, side), Position(market_id, side))
            if action == Action.BUY:
                total_cost = pos.avg_price * pos.qty + price * size
                pos.qty += size
                pos.avg_price = (total_cost / pos.qty) if pos.qty else 0.0
            else:  # SELL
                sold = min(size, pos.qty)
                realized = (price - pos.avg_price) * sold
                pos.qty = max(0.0, pos.qty - size)
                pos.realized_pnl += realized
                if pos.qty == 0.0:
                    pos.avg_price = 0.0
        self._log.debug("fill applied", extra={"market": market_id, "side": side,
                                                "action": action.value, "price": price,
                                                "size": size, "realized": round(realized, 6)})
        return realized

    # -- pnl / snapshot --------------------------------------------------------
    def realized_pnl(self) -> float:
        with self._lock:
            return sum(p.realized_pnl for p in self._positions.values())

    def snapshot(self, mark_prices: dict[str, float] | None = None) -> dict[str, Any]:
        """Position + PnL snapshot; ``mark_prices`` maps token_id -> price."""
        mark_prices = mark_prices or {}
        with self._lock:
            positions = list(self._positions.values())
        unrealized = 0.0
        rows = []
        for p in positions:
            if p.qty <= 0:
                continue
            state = self._registry.state(p.market_id)
            token_id = state.token_id(p.side) if state else ""
            mark = mark_prices.get(token_id, p.avg_price)
            u = p.unrealized(mark)
            unrealized += u
            rows.append({"market": p.market_id, "side": p.side, "qty": p.qty,
                         "avg_price": round(p.avg_price, 4), "unrealized": round(u, 4)})
        realized = sum(p.realized_pnl for p in positions)
        return {
            "positions": rows,
            "open_positions": len(rows),
            "realized_pnl": round(realized, 4),
            "unrealized_pnl": round(unrealized, 4),
            "total_pnl": round(realized + unrealized, 4),
            "exposure": round(sum(p.qty * p.avg_price for p in positions), 4),
        }
