"""Offline tests for polybot/exchange/market_discovery.py.

Uses a fake Gamma client (duck-typed .get()) instead of a real RestClient --
no network calls, mirrors the pagination/dedup/ranking contract the engine's
discovery loop depends on.
"""
from __future__ import annotations

import asyncio

from polybot.api.rest_client import ClientError
from polybot.exchange.market_discovery import discover_eligible_markets, fetch_active_rows

ASSETS = ("BTC", "ETH", "SOL", "XRP")
DURATIONS = ("5m", "15m")


def _row(condition_id, slug, question="", **overrides):
    base = {
        "conditionId": condition_id, "slug": slug, "question": question,
        "clobTokenIds": '["UP", "DOWN"]',
        "orderPriceMinTickSize": "0.01", "orderMinSize": "5",
        "negRisk": False, "acceptingOrders": True,
    }
    base.update(overrides)
    return base


class _FakeGamma:
    def __init__(self, pages: list[list[dict]], raise_offset_too_large_after: int | None = None):
        self._pages = pages
        self._raise_after = raise_offset_too_large_after
        self.calls: list[dict] = []

    async def get(self, path, *, params=None):
        self.calls.append(dict(params or {}))
        offset = (params or {}).get("offset", 0)
        page_index = offset // 100
        if self._raise_after is not None and page_index >= self._raise_after:
            raise ClientError(422, "offset too large", "https://gamma/markets")
        if page_index < len(self._pages):
            return self._pages[page_index]
        return []


def test_fetch_active_rows_paginates_until_short_page():
    page0 = [_row(f"c{i}", f"btc-updown-5m-{1_900_000_000 + i}") for i in range(100)]
    page1 = [_row("c100", "eth-updown-5m-1900000100")]
    gamma = _FakeGamma([page0, page1])
    rows = asyncio.run(fetch_active_rows(gamma))
    assert len(rows) == 101
    assert gamma.calls[0]["offset"] == 0
    assert gamma.calls[1]["offset"] == 100


def test_fetch_active_rows_stops_cleanly_on_offset_too_large():
    page0 = [_row(f"c{i}", f"btc-updown-5m-{1_900_000_000 + i}") for i in range(100)]
    gamma = _FakeGamma([page0, page0, page0], raise_offset_too_large_after=1)
    rows = asyncio.run(fetch_active_rows(gamma))
    assert len(rows) == 100  # only the first page, then it stopped gracefully


def test_discover_eligible_markets_filters_and_ranks_by_soonest_end():
    now = 2_000_000_000
    gamma = _FakeGamma([[
        _row("c-btc-far", f"btc-updown-5m-{now + 10_000}"),   # ends later
        _row("c-btc-near", f"btc-updown-5m-{now}"),           # ends sooner
        _row("c-doge", f"doge-updown-5m-{now}", question="Dogecoin"),  # wrong asset
        _row("c-btc-1h", f"btc-updown-1h-{now}"),             # wrong duration
    ]])
    result = asyncio.run(discover_eligible_markets(gamma, assets=ASSETS, durations=DURATIONS))
    assert [m.condition_id for m in result] == ["c-btc-near", "c-btc-far"]
    assert result[0].asset == "BTC" and result[0].duration == "5m"


def test_discover_eligible_markets_dedupes_by_condition_id():
    now = 2_000_000_000
    gamma = _FakeGamma([[
        _row("c-dup", f"btc-updown-5m-{now}"),
        _row("c-dup", f"btc-updown-5m-{now}"),
    ]])
    result = asyncio.run(discover_eligible_markets(gamma, assets=ASSETS, durations=DURATIONS))
    assert len(result) == 1


def test_discover_eligible_markets_empty_config_is_a_noop():
    gamma = _FakeGamma([[_row("c1", "btc-updown-5m-2000000000")]])
    result = asyncio.run(discover_eligible_markets(gamma, assets=(), durations=DURATIONS))
    assert result == []
    assert gamma.calls == []  # must not even hit the network when disabled

    result = asyncio.run(discover_eligible_markets(gamma, assets=ASSETS, durations=()))
    assert result == []
    assert gamma.calls == []
