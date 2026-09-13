"""Market-data contract — bound to the compiled strategy as top-level ``market_data``.

The compiled strategy imports ``from market_data import MarketState`` and reads,
each tick::

    market.market_id       # str identifier
    market.latest          # snapshot ("bar") or None
    market.elapsed         # seconds since the market opened / tracking began
    market.time_remaining  # seconds until the market closes

and from ``market.latest``::

    bar.up_bid, bar.up_ask, bar.down_bid, bar.down_ask   # best prices per side

This module provides those exact shapes plus the plumbing the live engine uses to
keep them fresh from the order book. It contains no strategy logic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Bar:
    """Best bid/ask snapshot for both outcome tokens of a binary market."""

    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    timestamp: float

    def __bool__(self) -> bool:
        # The strategy does ``if not bar: return``. Treat a snapshot with no
        # tradeable price on either side as falsy.
        return any(v > 0 for v in (self.up_bid, self.up_ask, self.down_bid, self.down_ask))


class MarketState:
    """Live, mutable view of a single binary market.

    ``up`` and ``down`` map to the market's two outcome tokens (index 0 and 1 of
    the market's ``clobTokenIds``). The live engine calls :meth:`update_side` as
    order-book messages arrive; the strategy reads :attr:`latest`.
    """

    def __init__(
        self,
        market_id: str,
        *,
        up_token_id: str,
        down_token_id: str,
        open_ts: float | None = None,
        close_ts: float | None = None,
        tick_size: float = 0.01,
        min_order_size: float = 5.0,
        neg_risk: bool = False,
    ) -> None:
        self.market_id = market_id
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.open_ts = time.time() if open_ts is None else open_ts
        self.close_ts = close_ts
        self.tick_size = tick_size
        self.min_order_size = min_order_size
        self.neg_risk = neg_risk
        # best prices per side; 0.0 means "no quote"
        self._up_bid = 0.0
        self._up_ask = 0.0
        self._down_bid = 0.0
        self._down_ask = 0.0
        self._last_update = 0.0

    # -- token/side mapping ----------------------------------------------------
    def token_id(self, side: str) -> str:
        return self.up_token_id if side == "up" else self.down_token_id

    def side_for_token(self, token_id: str) -> str | None:
        if token_id == self.up_token_id:
            return "up"
        if token_id == self.down_token_id:
            return "down"
        return None

    # -- updates ---------------------------------------------------------------
    def update_side(self, side: str, *, bid: float, ask: float, ts: float | None = None) -> None:
        """Record the latest best bid/ask for one side of the market."""
        if side == "up":
            self._up_bid, self._up_ask = bid, ask
        elif side == "down":
            self._down_bid, self._down_ask = bid, ask
        else:
            raise ValueError(f"unknown side {side!r}")
        self._last_update = time.time() if ts is None else ts

    # -- reads consumed by the strategy ---------------------------------------
    @property
    def latest(self) -> Bar | None:
        """Most recent snapshot, or ``None`` if no book update has arrived yet."""
        if self._last_update == 0.0:
            return None
        return Bar(
            up_bid=self._up_bid,
            up_ask=self._up_ask,
            down_bid=self._down_bid,
            down_ask=self._down_ask,
            timestamp=self._last_update,
        )

    @property
    def elapsed(self) -> float:
        """Seconds since the market opened / tracking began (never negative)."""
        return max(0.0, time.time() - self.open_ts)

    @property
    def time_remaining(self) -> float:
        """Seconds until the market closes; ``inf`` if no close time is known."""
        if self.close_ts is None:
            return float("inf")
        return max(0.0, self.close_ts - time.time())

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"MarketState(market_id={self.market_id!r}, up={self._up_bid}/{self._up_ask}, "
            f"down={self._down_bid}/{self._down_ask}, elapsed={self.elapsed:.0f}s, "
            f"time_remaining={self.time_remaining:.0f}s)"
        )
