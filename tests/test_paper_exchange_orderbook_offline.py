"""Offline regression tests for PaperExchange's CLOB v2 order-book parsing bug.

py-clob-client-v2's get_order_book() returns a plain dict (bids/asks are lists
of {"price", "size"} dicts), not an attribute-style object.
PaperExchange.refresh_depth() used getattr(book, "bids", None) / "asks", which
silently defaulted to [] against a dict -- the paper matching engine's own
depth cache never saw real liquidity, even though _levels()/apply_book()
already handled dict-shaped levels correctly downstream.
"""
from __future__ import annotations

import asyncio

from polybot.orders.models import Action, OrderIntent
from polybot.paper.account import PaperAccount
from polybot.paper.exchange import PaperExchange

TOKEN = "69324317355037271422943965141382095011871956039434394956830818206664869608517"


class _FakeV2Reader:
    """Stand-in for the keyless public ClobExchange reader."""

    def __init__(self, book: dict) -> None:
        self._book = book

    async def get_order_book(self, token_id):
        return self._book


def _populated_book() -> dict:
    return {
        "market": "0xMARKET", "asset_id": TOKEN,
        "bids": [{"price": "0.01", "size": "5534.22"}, {"price": "0.02", "size": "10"}],
        "asks": [{"price": "0.99", "size": "5574.33"}, {"price": "0.95", "size": "10"}],
        "min_order_size": "5", "tick_size": "0.01", "neg_risk": False,
    }


def _empty_book() -> dict:
    return {"market": "0xMARKET", "asset_id": TOKEN, "bids": [], "asks": []}


def _exchange(book: dict) -> PaperExchange:
    return PaperExchange(_FakeV2Reader(book), PaperAccount(1000.0))


def test_refresh_depth_parses_populated_v2_dict_book():
    exchange = _exchange(_populated_book())
    asyncio.run(exchange.refresh_depth(TOKEN))
    book = exchange._books[TOKEN]
    assert book["bids"] == [(0.02, 10.0), (0.01, 5534.22)]  # sorted best-first
    assert book["asks"] == [(0.95, 10.0), (0.99, 5574.33)]


def test_refresh_depth_handles_empty_book_safely():
    exchange = _exchange(_empty_book())
    asyncio.run(exchange.refresh_depth(TOKEN))
    book = exchange._books[TOKEN]
    assert book["bids"] == []
    assert book["asks"] == []


def test_refresh_depth_handles_missing_keys_safely():
    exchange = _exchange({"market": "0xMARKET", "asset_id": TOKEN})
    asyncio.run(exchange.refresh_depth(TOKEN))
    book = exchange._books[TOKEN]
    assert book["bids"] == []
    assert book["asks"] == []


def test_paper_order_matches_against_real_refreshed_depth():
    """End-to-end: refresh_depth -> place_order -> match() produces a real fill."""
    exchange = _exchange(_populated_book())
    asyncio.run(exchange.refresh_depth(TOKEN))

    intent = OrderIntent(market_id="0xMARKET", strategy_side="up", token_id=TOKEN,
                         action=Action.BUY, price=0.99, size=10.0)
    result = asyncio.run(exchange.place_order(intent))
    assert result["success"] is True, result

    import time
    time.sleep(0.3)  # clear the default paper_fill_latency_ms window
    fills = exchange.match()
    assert len(fills) == 1
    assert fills[0]["price"] <= 0.99
    assert fills[0]["size"] == 10.0
