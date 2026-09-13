"""Offline integration test for the live engine (no network, dry-run).

Exercises Broker -> OrderManager -> RiskEngine -> PortfolioManager end-to-end by
driving the compiled strategy against a manually-registered market, asserting:
simulated order submission, optimistic notional accounting, duplicate
suppression, risk rejection, and market cancel. Runnable directly or under pytest.
"""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("MAX_TRADE_VALUE", "5")

from polybot.config.settings import Settings  # noqa: E402
from polybot.exchange.market_registry import MarketInfo, MarketRegistry  # noqa: E402
from polybot.execution.executor import Broker  # noqa: E402
from polybot.monitoring.metrics import Metrics  # noqa: E402
from polybot.orders.manager import OrderManager  # noqa: E402
from polybot.portfolio.manager import PortfolioManager  # noqa: E402
from polybot.risk.engine import RiskEngine  # noqa: E402
from polybot.strategy.loader import load_strategy_class  # noqa: E402
from polybot.strategy.market_data import MarketState  # noqa: E402

MID = "0xMARKET"


class FakeBalance:
    balance = 10_000.0
    def has_funds_for(self, notional): return True
    async def refresh(self, *, force=False): return self.balance


def _registry_with_market() -> MarketRegistry:
    reg = MarketRegistry(exchange=None)  # type: ignore[arg-type]
    now = time.time()
    state = MarketState(MID, up_token_id="U", down_token_id="D", open_ts=now - 60,
                        close_ts=now + 3600, tick_size=0.01, min_order_size=5.0)
    state.update_side("up", bid=0.005, ask=0.02)
    state.update_side("down", bid=0.005, ask=0.02)
    reg._states[MID] = state
    reg._info[MID] = MarketInfo(MID, "U", "D", "Up", "Down", 0.01, 5.0, False, now + 3600, True)
    reg._token_index["U"] = (MID, "up")
    reg._token_index["D"] = (MID, "down")
    return reg


def _build():
    settings = Settings.load()
    reg = _registry_with_market()
    portfolio = PortfolioManager(reg, funder_address="0xFUND", data_api=None)
    risk = RiskEngine(settings, portfolio=portfolio, balance=FakeBalance())
    orders = OrderManager(exchange=None, registry=reg, risk=risk, portfolio=portfolio,  # type: ignore[arg-type]
                          dry_run=True, dedup_window_s=settings.dedup_window_s)
    risk.bind_orders(orders)
    broker = Broker(orders, reg, portfolio)
    strat = load_strategy_class()(broker=broker, risk=risk, portfolio=None)
    return settings, reg, portfolio, risk, orders, broker, strat


async def _drain(orders: OrderManager, seconds: float = 0.4) -> None:
    await asyncio.sleep(seconds)


async def _scenario() -> None:
    settings, reg, portfolio, risk, orders, broker, strat = _build()
    orders.bind_loop(asyncio.get_running_loop())
    run_task = asyncio.create_task(orders.run())
    try:
        # 1) entry tick -> six simulated resting bids (1c/2c/3c on both sides).
        # With PER_ORDER_NOTIONAL = MAX_TRADE_VALUE/3, all three tiers fit inside
        # the strategy's notional_ok(..., 2*MAX_TRADE_VALUE) cap.
        await asyncio.to_thread(strat.on_bar, reg.state(MID))
        await _drain(orders)
        assert orders.open_count() == 6, orders.open_count()
        assert all(o.simulated for o in orders.open_orders())
        notional = orders.market_open_notional(MID)
        assert 9.0 < notional <= 10.0, notional  # ~2 x MAX_TRADE_VALUE

        # 2) optimistic accounting is visible to the strategy via the broker
        assert abs(broker.notional_in_market(MID) - notional) < 1e-6

        # 3) duplicate suppression (re-submit an existing order within the window)
        existing = orders.open_orders()[0].intent
        before = orders.open_count()
        broker.place_limit(MID, existing.strategy_side, existing.price, existing.size)
        await _drain(orders)
        assert orders.open_count() == before

        # 4) risk rejection: shrink the per-order cap; an oversized order is refused
        settings.max_order_value = 1.0
        rejected = broker.place_limit(MID, "down", 0.02, 250)  # 5.0 notional > 1.0
        assert rejected is None
        settings.max_order_value = 5.0

        # 5) cancel all open orders in the market
        broker.cancel_all_open(MID)
        await _drain(orders)
        assert orders.open_count() == 0
        assert orders.market_open_notional(MID) == 0.0
    finally:
        orders.stop()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


def test_engine_offline():
    asyncio.run(_scenario())


if __name__ == "__main__":
    test_engine_offline()
    print("engine offline integration: ALL PASSED ✅")
