"""Offline regression test for ClobExchange.best_bid_ask's v2 dict parsing.

py-clob-client-v2's get_order_book() returns a plain dict (bids/asks are lists
of {"price", "size"} dicts), not an attribute-style object. This locks in that
best_bid_ask() reads the real v2 shape instead of silently defaulting to 0.0.
"""
from __future__ import annotations

import asyncio

from polybot.exchange.clob import ClobExchange
from polybot.monitoring.metrics import Metrics
from polybot.utils.retry import RetryPolicy


class _FakeV2Client:
    """Stand-in for py_clob_client_v2.client.ClobClient's get_order_book."""

    def get_order_book(self, token_id):
        return {
            "market": "0xMARKET",
            "asset_id": token_id,
            "bids": [{"price": "0.01", "size": "5534.22"}, {"price": "0.05", "size": "10"}],
            "asks": [{"price": "0.99", "size": "5574.33"}, {"price": "0.95", "size": "10"}],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
            "last_trade_price": "0.5",
        }


def test_best_bid_ask_parses_v2_dict_response():
    exchange = ClobExchange(_FakeV2Client(), signature_type=0, retry_policy=RetryPolicy(attempts=1),
                            metrics=Metrics())
    result = asyncio.run(exchange.best_bid_ask("token-123"))
    assert result["bid"] == 0.05
    assert result["ask"] == 0.95
    assert result["tick_size"] == 0.01
    assert result["min_order_size"] == 5.0
    assert result["neg_risk"] is False


def test_best_bid_ask_empty_book_defaults_to_zero():
    class _EmptyBookClient:
        def get_order_book(self, token_id):
            return {"bids": [], "asks": []}

    exchange = ClobExchange(_EmptyBookClient(), signature_type=0, retry_policy=RetryPolicy(attempts=1),
                            metrics=Metrics())
    result = asyncio.run(exchange.best_bid_ask("token-123"))
    assert result["bid"] == 0.0
    assert result["ask"] == 0.0
