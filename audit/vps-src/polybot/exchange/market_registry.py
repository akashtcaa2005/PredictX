"""Market registry — resolves markets and maintains their live :class:`MarketState`.

Given a market's ``condition_id``, we fetch metadata from the CLOB (the two
outcome tokens, tick size, minimum order size, neg-risk flag, close time) and
build a :class:`~polybot.strategy.market_data.MarketState`. Outcome token index
0 maps to ``"up"`` and index 1 to ``"down"`` — the two sides the strategy trades.
Book updates (from REST snapshots or the websocket) flow back in here to keep the
snapshots fresh.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from polybot.exchange.clob import ClobExchange
from polybot.strategy.market_data import MarketState
from polybot.utils.time_utils import iso_to_ts, now_s


@dataclass
class MarketInfo:
    """Static-ish metadata for one market."""

    condition_id: str
    up_token_id: str
    down_token_id: str
    up_outcome: str
    down_outcome: str
    tick_size: float
    min_order_size: float
    neg_risk: bool
    close_ts: float | None
    accepting_orders: bool


class MarketRegistry:
    """Owns the set of tradeable markets and their live state."""

    def __init__(self, exchange: ClobExchange, logger: logging.Logger | None = None) -> None:
        self._exchange = exchange
        self._log = logger or logging.getLogger("polybot.exchange.registry")
        self._info: dict[str, MarketInfo] = {}
        self._states: dict[str, MarketState] = {}
        self._token_index: dict[str, tuple[str, str]] = {}  # token_id -> (market_id, side)

    async def register(self, condition_id: str) -> MarketState:
        """Fetch metadata for ``condition_id`` and create its :class:`MarketState`."""
        market = await self._exchange.get_market(condition_id)
        info = self._parse_market(condition_id, market)
        if info.tick_size <= 0:
            info.tick_size = await self._exchange.get_tick_size(info.up_token_id)

        state = MarketState(
            market_id=condition_id,
            up_token_id=info.up_token_id,
            down_token_id=info.down_token_id,
            open_ts=now_s(),
            close_ts=info.close_ts,
            tick_size=info.tick_size,
            min_order_size=info.min_order_size,
            neg_risk=info.neg_risk,
        )
        self._info[condition_id] = info
        self._states[condition_id] = state
        self._token_index[info.up_token_id] = (condition_id, "up")
        self._token_index[info.down_token_id] = (condition_id, "down")
        self._log.info(
            "registered market",
            extra={"market": condition_id, "up": info.up_outcome, "down": info.down_outcome,
                   "tick": info.tick_size, "neg_risk": info.neg_risk,
                   "accepting_orders": info.accepting_orders, "close_ts": info.close_ts},
        )
        return state

    def _parse_market(self, condition_id: str, market: dict[str, Any]) -> MarketInfo:
        tokens = market.get("tokens") or []
        if len(tokens) < 2:
            raise ValueError(f"market {condition_id} does not have two outcome tokens")
        up, down = tokens[0], tokens[1]
        return MarketInfo(
            condition_id=condition_id,
            up_token_id=str(up.get("token_id")),
            down_token_id=str(down.get("token_id")),
            up_outcome=str(up.get("outcome", "up")),
            down_outcome=str(down.get("outcome", "down")),
            tick_size=float(market.get("minimum_tick_size") or market.get("tick_size") or 0.0),
            min_order_size=float(market.get("minimum_order_size") or 0.0),
            neg_risk=bool(market.get("neg_risk", False)),
            close_ts=iso_to_ts(market.get("end_date_iso")),
            accepting_orders=bool(market.get("accepting_orders", True)),
        )

    # -- accessors -------------------------------------------------------------
    def state(self, market_id: str) -> MarketState | None:
        return self._states.get(market_id)

    def info(self, market_id: str) -> MarketInfo | None:
        return self._info.get(market_id)

    def all_states(self) -> list[MarketState]:
        return list(self._states.values())

    def all_token_ids(self) -> list[str]:
        return list(self._token_index)

    def resolve_token(self, token_id: str) -> tuple[str, str] | None:
        """Return ``(market_id, side)`` for a token id, or ``None``."""
        return self._token_index.get(token_id)

    # -- book updates ----------------------------------------------------------
    async def refresh_book(self, market_id: str) -> None:
        """Pull a fresh REST book snapshot for both sides of a market."""
        state = self._states.get(market_id)
        if state is None:
            return
        for side in ("up", "down"):
            quote = await self._exchange.best_bid_ask(state.token_id(side))
            state.update_side(side, bid=quote["bid"], ask=quote["ask"])

    def apply_book(self, token_id: str, *, bids: Iterable[Any], asks: Iterable[Any],
                   ts: float | None = None) -> None:
        """Apply a websocket/REST book to the matching market side."""
        resolved = self._token_index.get(token_id)
        if resolved is None:
            return
        market_id, side = resolved
        state = self._states.get(market_id)
        if state is None:
            return
        best_bid = max((float(_price(x)) for x in bids), default=0.0)
        best_ask = min((float(_price(x)) for x in asks), default=0.0)
        state.update_side(side, bid=best_bid, ask=best_ask, ts=ts)


def _price(level: Any) -> float:
    """Extract a price from an order-book level (dict, object, or [price, size])."""
    if isinstance(level, dict):
        return float(level.get("price", 0.0))
    if isinstance(level, (list, tuple)):
        return float(level[0])
    return float(getattr(level, "price", 0.0))
