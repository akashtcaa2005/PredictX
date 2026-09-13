"""Offline regression tests for the WebSocket ``price_change`` market-data bug.

Polymarket's market WS channel sends two relevant event types:

* ``event_type="book"``   — full snapshot, top-level asset_id/bids/asks.
* ``event_type="price_change"`` — incremental update, shaped
  ``{"market": ..., "price_changes": [...], "timestamp": ..., "event_type": ...}``,
  where each entry in ``price_changes`` carries its own ``asset_id``,
  ``best_bid`` and ``best_ask`` (captured from the real venue — see
  Docs/ or the session notes for the raw payload this was modeled on).

Previously ``_on_market_event`` only handled the "book" shape (it looked for a
top-level ``asset_id``), so every ``price_change`` event was silently dropped.
These tests exercise the real production dispatchers (``TradingBot`` and
``PaperTradingBot``'s ``_on_market_event``) and the new
``MarketRegistry.apply_price_change`` directly.
"""
from __future__ import annotations

import asyncio
import time

from polybot.exchange.market_registry import MarketInfo, MarketRegistry
from polybot.main import TradingBot
from polybot.paper.main import PaperTradingBot

MID = "0xMARKET"
UP = "69324317355037271422943965141382095011871956039434394956830818206664869608517"
DOWN = "51797157743046504218541616681751597845468055908324407922581755135522797852101"

# A realistic price_change message, shaped exactly like the raw payload observed
# on the live wss://ws-subscriptions-clob.polymarket.com/ws/market channel.
REAL_PRICE_CHANGE_EVENT = {
    "market": "0x0b4cc3b739e1dfe5d73274740e7308b6fb389c5af040c3a174923d928d134bee",
    "price_changes": [
        {
            "asset_id": UP,
            "price": "0.002",
            "size": "969430.42",
            "side": "BUY",
            "hash": "70cb34f73b950a453cdf9ca77de32a18fd98a645",
            "best_bid": "0.019",
            "best_ask": "0.02",
        },
        {
            "asset_id": DOWN,
            "price": "0.998",
            "size": "969430.42",
            "side": "SELL",
            "hash": "9e73fb57d082118874f97a6e29c26d2f0ff53ba9",
            "best_bid": "0.98",
            "best_ask": "0.981",
        },
    ],
    "timestamp": "1786167920518",
    "event_type": "price_change",
}

REAL_BOOK_EVENT = {
    "market": "0x0b4cc3b739e1dfe5d73274740e7308b6fb389c5af040c3a174923d928d134bee",
    "asset_id": UP,
    "timestamp": "1786161905768",
    "hash": "d40106c99c06926b9b80208d93820c406d16db58",
    "bids": [{"price": "0.01", "size": "5534.22"}],
    "asks": [{"price": "0.02", "size": "5574.33"}],
    "tick_size": "0.01",
    "event_type": "book",
}


def _registry() -> MarketRegistry:
    reg = MarketRegistry(exchange=None)  # type: ignore[arg-type]
    now = time.time()
    reg._info[MID] = MarketInfo(MID, UP, DOWN, "Up", "Down", 0.01, 5.0, False, now + 3600, True)
    reg._token_index[UP] = (MID, "up")
    reg._token_index[DOWN] = (MID, "down")
    from polybot.strategy.market_data import MarketState
    state = MarketState(MID, up_token_id=UP, down_token_id=DOWN, open_ts=now - 60,
                        close_ts=now + 3600, tick_size=0.01, min_order_size=5.0)
    reg._states[MID] = state
    return reg


class _FakeSelf:
    """Duck-typed ``self`` so we can call the real bound _on_market_event."""

    def __init__(self, registry, exchange=None):
        self.registry = registry
        self.exchange = exchange


class _NoopExchange:
    def apply_book(self, token_id, *, bids, asks):
        pass


# ==========================================================================
# MarketRegistry.apply_price_change — unit level
# ==========================================================================
def test_apply_price_change_updates_correct_asset():
    reg = _registry()
    reg.apply_price_change(UP, best_bid="0.019", best_ask="0.02", ts=1000.0)
    up_state = reg.state(MID)
    assert up_state.latest.up_bid == 0.019
    assert up_state.latest.up_ask == 0.02
    # the other side must be untouched (still 0.0 — no update yet)
    assert up_state.latest.down_bid == 0.0
    assert up_state.latest.down_ask == 0.0


def test_apply_price_change_multiple_entries_update_both_sides():
    reg = _registry()
    for change in REAL_PRICE_CHANGE_EVENT["price_changes"]:
        reg.apply_price_change(change["asset_id"], best_bid=change["best_bid"],
                               best_ask=change["best_ask"], ts=2000.0)
    bar = reg.state(MID).latest
    assert bar.up_bid == 0.019 and bar.up_ask == 0.02
    assert bar.down_bid == 0.98 and bar.down_ask == 0.981


def test_apply_price_change_updates_last_update_timestamp():
    reg = _registry()
    assert reg.state(MID).latest is None  # nothing recorded yet
    reg.apply_price_change(UP, best_bid="0.5", best_ask="0.51", ts=12345.5)
    assert reg.state(MID).latest.timestamp == 12345.5
    reg.apply_price_change(UP, best_bid="0.5", best_ask="0.52", ts=12400.0)
    assert reg.state(MID).latest.timestamp == 12400.0


def test_apply_price_change_unknown_token_is_a_safe_noop():
    reg = _registry()
    reg.apply_price_change("0xNOT-A-TRACKED-TOKEN", best_bid="0.5", best_ask="0.51", ts=1.0)
    assert reg.state(MID).latest is None


def test_apply_price_change_malformed_prices_are_a_safe_noop():
    reg = _registry()
    for bad_bid, bad_ask in [(None, None), ("not-a-number", "0.5"), ("0.5", None)]:
        reg.apply_price_change(UP, best_bid=bad_bid, best_ask=bad_ask, ts=1.0)
    assert reg.state(MID).latest is None  # never successfully updated


# ==========================================================================
# Real dispatcher (_on_market_event) — live engine
# ==========================================================================
def test_live_dispatcher_handles_price_change_event():
    reg = _registry()
    fake_self = _FakeSelf(reg)
    asyncio.run(TradingBot._on_market_event(fake_self, REAL_PRICE_CHANGE_EVENT))
    bar = reg.state(MID).latest
    assert bar is not None
    assert bar.up_bid == 0.019 and bar.up_ask == 0.02
    assert bar.down_bid == 0.98 and bar.down_ask == 0.981


def test_live_dispatcher_still_handles_book_event():
    reg = _registry()
    fake_self = _FakeSelf(reg)
    asyncio.run(TradingBot._on_market_event(fake_self, REAL_BOOK_EVENT))
    bar = reg.state(MID).latest
    assert bar.up_bid == 0.01 and bar.up_ask == 0.02


def test_live_dispatcher_ignores_malformed_price_change_message():
    reg = _registry()
    fake_self = _FakeSelf(reg)
    malformed = {"event_type": "price_change", "price_changes": "not-a-list"}
    asyncio.run(TradingBot._on_market_event(fake_self, malformed))
    assert reg.state(MID).latest is None

    malformed_entries = {"event_type": "price_change", "price_changes": [
        {"no_asset_id_here": True}, {"asset_id": UP, "best_bid": "oops", "best_ask": "0.5"},
    ]}
    asyncio.run(TradingBot._on_market_event(fake_self, malformed_entries))
    assert reg.state(MID).latest is None


# ==========================================================================
# Real dispatcher (_on_market_event) — paper engine
# ==========================================================================
def test_paper_dispatcher_handles_price_change_event():
    reg = _registry()
    fake_self = _FakeSelf(reg, exchange=_NoopExchange())
    asyncio.run(PaperTradingBot._on_market_event(fake_self, REAL_PRICE_CHANGE_EVENT))
    bar = reg.state(MID).latest
    assert bar.up_bid == 0.019 and bar.down_bid == 0.98


def test_paper_dispatcher_still_handles_book_event():
    reg = _registry()
    fake_self = _FakeSelf(reg, exchange=_NoopExchange())
    asyncio.run(PaperTradingBot._on_market_event(fake_self, REAL_BOOK_EVENT))
    bar = reg.state(MID).latest
    assert bar.up_bid == 0.01 and bar.up_ask == 0.02
