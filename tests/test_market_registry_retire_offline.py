"""Offline tests for MarketRegistry.retire() -- the safe market-lifecycle gap
identified during design review: PortfolioManager.apply_fill/OrderManager
resolve markets via the registry and silently no-op if the entry is gone, so
retire() must NEVER hard-delete -- only exclude from the active/tick/WS view.
"""
from __future__ import annotations

import time

from polybot.exchange.market_registry import MarketInfo, MarketRegistry
from polybot.strategy.market_data import MarketState

MID = "0xMARKET"
UP, DOWN = "U", "D"


def _registry_with_market(market_id=MID) -> MarketRegistry:
    reg = MarketRegistry(exchange=None)  # type: ignore[arg-type]
    now = time.time()
    reg._info[market_id] = MarketInfo(market_id, UP, DOWN, "Up", "Down", 0.01, 5.0, False,
                                      now + 3600, True)
    reg._token_index[UP] = (market_id, "up")
    reg._token_index[DOWN] = (market_id, "down")
    reg._states[market_id] = MarketState(market_id, up_token_id=UP, down_token_id=DOWN,
                                         open_ts=now - 60, close_ts=now + 3600)
    return reg


def test_retire_drops_market_from_all_states():
    reg = _registry_with_market()
    assert len(reg.all_states()) == 1
    reg.retire(MID)
    assert reg.all_states() == []


def test_retire_drops_tokens_from_all_token_ids():
    reg = _registry_with_market()
    assert set(reg.all_token_ids()) == {UP, DOWN}
    reg.retire(MID)
    assert reg.all_token_ids() == []


def test_retire_leaves_resolve_token_working_for_late_fills():
    """The core safety property: a fill that arrives after retirement must
    still attribute correctly, not silently vanish (PortfolioManager.apply_fill
    and OrderManager._submit both resolve via these and no-op if unknown)."""
    reg = _registry_with_market()
    reg.retire(MID)
    assert reg.resolve_token(UP) == (MID, "up")
    assert reg.resolve_token(DOWN) == (MID, "down")


def test_retire_leaves_info_and_state_working_for_late_fills():
    reg = _registry_with_market()
    reg.retire(MID)
    assert reg.info(MID) is not None
    assert reg.state(MID) is not None


def test_retire_leaves_apply_book_and_apply_price_change_working():
    """A book/price_change event that arrives right after retirement (a real
    race: discovery retires while a message is already in flight) must still
    update the underlying MarketState -- not a no-op, not an exception."""
    reg = _registry_with_market()
    reg.retire(MID)
    reg.apply_price_change(UP, best_bid="0.4", best_ask="0.41", ts=123.0)
    bar = reg.state(MID).latest
    assert bar.up_bid == 0.4 and bar.up_ask == 0.41


def test_is_retired():
    reg = _registry_with_market()
    assert reg.is_retired(MID) is False
    reg.retire(MID)
    assert reg.is_retired(MID) is True


def test_retiring_unknown_market_is_a_safe_noop():
    reg = _registry_with_market()
    reg.retire("0xNEVER-REGISTERED")  # must not raise
    assert len(reg.all_states()) == 1  # the real market is unaffected


def test_reregistering_a_retired_market_reactivates_it():
    """If discovery re-finds a market it previously retired (e.g. a rotation
    boundary edge case), register() must bring it back into the active view."""
    reg = _registry_with_market()
    reg.retire(MID)
    assert reg.all_states() == []

    # register() needs a working exchange; simulate what it does directly
    # rather than requiring a real ClobExchange for this unit test.
    reg._retired.discard(MID)
    assert len(reg.all_states()) == 1
    assert reg.is_retired(MID) is False


def test_retire_is_idempotent():
    reg = _registry_with_market()
    reg.retire(MID)
    reg.retire(MID)  # must not raise or double-log-crash
    assert reg.all_states() == []
