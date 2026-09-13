"""Offline tests for polybot/exchange/market_classify.py.

Ported logic from dashboard/market_feed.py's discovery/classification — these
tests exercise the same behavior the dashboard already relies on, now applied
to the engine's own market-discovery path.
"""
from __future__ import annotations

import time

from polybot.exchange.market_classify import (
    classify, match_asset, match_duration, parse_window,
)

ASSETS = ("BTC", "ETH", "SOL", "XRP")
DURATIONS = ("5m", "15m")


def test_match_asset_word_boundary():
    assert match_asset("btc-updown-5m-123", ASSETS) == "BTC"
    assert match_asset("ethereum up or down", ASSETS) == "ETH"
    assert match_asset("solana price prediction", ASSETS) == "SOL"
    assert match_asset("ripple xrp market", ASSETS) == "XRP"
    assert match_asset("something else entirely", ASSETS) is None


def test_match_asset_rejects_substring_false_positives():
    # "eth" must not match inside an unrelated word like "method" or "wealth".
    assert match_asset("methodology market", ("ETH",)) is None
    assert match_asset("generational wealth index", ("ETH",)) is None


def test_parse_window_extracts_duration_and_bounds():
    result = parse_window("btc-updown-5m-1766161800")
    assert result == ("5m", 1766161800.0, 1766162100.0)  # +300s


def test_parse_window_handles_hour_unit():
    result = parse_window("eth-up-or-down-1h-1766161800")
    assert result == ("1h", 1766161800.0, 1766165400.0)  # +3600s


def test_parse_window_returns_none_for_unrelated_slug():
    assert parse_window("some-other-market-slug") is None


def test_match_duration_from_timestamps():
    start = 1_800_000_000.0
    end = start + 300  # exactly 5 minutes
    assert match_duration(start, end, "", DURATIONS) == "5m"


def test_match_duration_falls_back_to_text():
    assert match_duration(None, None, "btc 15m up or down", DURATIONS) == "15m"


def test_match_duration_rejects_unwanted_duration():
    start = 1_800_000_000.0
    end = start + 3600  # 1h span, but only 5m/15m wanted
    assert match_duration(start, end, "", DURATIONS) is None


def _row(**overrides) -> dict:
    base = {
        "slug": "btc-updown-5m-1900000000",
        "question": "Bitcoin Up or Down - 5 minutes",
        "conditionId": "0xCOND123",
        "clobTokenIds": '["111", "222"]',
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "negRisk": False,
        "acceptingOrders": True,
    }
    base.update(overrides)
    return base


def test_classify_accepts_valid_future_window(monkeypatch):
    # window end is far in the future (1900000000 + 300), so it's not closed.
    result = classify(_row(), ASSETS, DURATIONS)
    assert result is not None
    assert result.asset == "BTC"
    assert result.duration == "5m"
    assert result.condition_id == "0xCOND123"
    assert result.up_token == "111"
    assert result.down_token == "222"


def test_classify_rejects_unwanted_asset():
    row = _row(slug="doge-updown-5m-1900000000", question="Dogecoin up or down")
    assert classify(row, ASSETS, DURATIONS) is None


def test_classify_rejects_unwanted_duration():
    row = _row(slug="btc-updown-1h-1900000000", question="Bitcoin hourly")
    assert classify(row, ASSETS, DURATIONS) is None


def test_classify_rejects_already_closed_window():
    # A real-shaped (10-digit) unix timestamp, well in the past, so the
    # 5-minute window it encodes has long since closed.
    past_start = int(time.time()) - 10_000
    row = _row(slug=f"btc-updown-5m-{past_start}")
    assert classify(row, ASSETS, DURATIONS) is None


def test_classify_rejects_missing_tokens():
    row = _row(clobTokenIds="[]")
    assert classify(row, ASSETS, DURATIONS) is None


def test_classify_rejects_missing_condition_id():
    row = _row(conditionId="")
    assert classify(row, ASSETS, DURATIONS) is None


def test_classify_real_captured_payload_shape():
    """Modeled on the real 'Bitcoin Up or Down' market captured during this session."""
    row = _row(
        slug="",  # this real market had no slug, only a question
        question="Bitcoin Up or Down - August 8, 9:10PM-9:15PM ET",
        startDate="2026-08-08T00:00:00Z",
        endDate="2026-09-08T00:00:00Z",  # far-future Gamma resolution deadline
    )
    result = classify(row, ASSETS, DURATIONS)
    # No slug window and Gamma's start/end span ~1 month, not 5 minutes, so
    # duration can't be inferred from timestamps; falls back to text scan,
    # which also finds nothing explicit here — correctly rejected rather than
    # silently misclassified.
    assert result is None
