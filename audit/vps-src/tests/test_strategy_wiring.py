"""Wiring tests for the compiled strategy against mock adapters.

Proves the shipped bytecode loads and drives entry, tiered take-profit, the
idempotent take-profit guard, the end-of-market cancel, and the no-quote no-op —
without any network. Runnable directly (``python -m tests.test_strategy_wiring``)
or under pytest.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("MAX_TRADE_VALUE", "5")

from polybot.strategy.loader import load_strategy_class  # noqa: E402
from polybot.strategy.market_data import MarketState  # noqa: E402


class MockBroker:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.pos: dict[tuple[str, str], float] = {}

    def place_limit(self, m, s, p, q): self.calls.append(("place", m, s, round(p, 4), q))
    def sell(self, m, s, p, sz): self.calls.append(("sell", m, s, round(p, 4), sz))
    def position_qty(self, m, s): return self.pos.get((m, s), 0.0)
    def cancel_all_open(self, m): self.calls.append(("cancel_all", m))
    def notional_in_market(self, m): return 0.0


class MockRisk:
    def notional_ok(self, current, limit): return True


def _make(open_offset=-60, close_offset=3600):
    now = time.time()
    strat = load_strategy_class()(broker=(b := MockBroker()), risk=MockRisk())
    state = MarketState("MKT1", up_token_id="U", down_token_id="D",
                        open_ts=now + open_offset, close_ts=now + close_offset)
    return strat, b, state


def test_loads_expected_class():
    cls = load_strategy_class()
    assert cls.__name__ == "CheapBuyStrategy"
    assert cls.name == "1c_buy"


def test_entry_places_six_resting_bids():
    strat, b, state = _make()
    state.update_side("up", bid=0.005, ask=0.02)
    state.update_side("down", bid=0.005, ask=0.02)
    strat.on_bar(state)
    places = [c for c in b.calls if c[0] == "place"]
    assert len(places) == 6
    assert strat.market_state("MKT1")["entered"] is True


def test_tiered_take_profit_and_idempotence():
    strat, b, state = _make()
    state.update_side("up", bid=0.005, ask=0.02)
    state.update_side("down", bid=0.005, ask=0.02)
    strat.on_bar(state)  # entry (initialises tp_done)
    b.calls.clear()
    b.pos[("MKT1", "up")] = 100.0
    state.update_side("up", bid=0.15, ask=0.16)  # crosses both TP tiers (0.10, 0.15)
    strat.on_bar(state)
    sells = [c for c in b.calls if c[0] == "sell"]
    assert any(abs(c[4] - 50.0) < 1e-9 for c in sells)   # 50% at 0.10
    assert any(abs(c[4] - 100.0) < 1e-9 for c in sells)  # 100% at 0.15
    b.calls.clear()
    strat.on_bar(state)  # tp_done must suppress repeats
    assert [c for c in b.calls if c[0] == "sell"] == []


def test_cancel_inside_final_window():
    strat, b, state = _make()
    state.update_side("up", bid=0.005, ask=0.02)
    state.update_side("down", bid=0.005, ask=0.02)
    strat.on_bar(state)
    b.calls.clear()
    state.close_ts = time.time() + 10  # inside CANCEL_BEFORE_CLOSE_S (30s)
    strat.on_bar(state)
    assert ("cancel_all", "MKT1") in b.calls


def test_no_quote_is_noop():
    strat, b, state = _make()
    strat.on_bar(state)  # no update_side -> latest is None
    assert b.calls == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("strategy wiring: ALL PASSED ✅")
