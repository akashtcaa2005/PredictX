"""Offline tests for the engines' _run_discovery_cycle orchestration --
register/retire decisions, safe-eviction gating, and tag stamping. Exercises
the real bound methods on TradingBot/PaperTradingBot (not reimplementations),
with fake Gamma/CLOB/order/portfolio collaborators so no network is touched.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import polybot.main as live_mod
import polybot.paper.main as paper_mod
from polybot.exchange.market_classify import ClassifiedMarket
from polybot.exchange.market_registry import MarketRegistry
from polybot.main import TradingBot
from polybot.paper.main import PaperTradingBot
from polybot.telemetry.recorder import NullRecorder


class _FakeExchange:
    """Enough of ClobExchange's async surface for MarketRegistry.register()/refresh_book()."""

    def __init__(self):
        self.registered: list[str] = []

    async def get_market(self, condition_id: str):
        self.registered.append(condition_id)
        return {
            "tokens": [{"token_id": f"{condition_id}-UP", "outcome": "Up"},
                      {"token_id": f"{condition_id}-DOWN", "outcome": "Down"}],
            "minimum_tick_size": 0.01, "minimum_order_size": 5.0, "neg_risk": False,
            "end_date_iso": None, "accepting_orders": True,
        }

    async def get_tick_size(self, token_id: str) -> float:
        return 0.01

    async def best_bid_ask(self, token_id: str):
        return {"bid": 0.5, "ask": 0.51}


def _classified(condition_id, asset="BTC", duration="5m") -> ClassifiedMarket:
    return ClassifiedMarket(
        condition_id=condition_id, asset=asset, duration=duration,
        question="q", slug="s", up_token=f"{condition_id}-UP", down_token=f"{condition_id}-DOWN",
        tick_size=0.01, min_order_size=5.0, neg_risk=False,
        end_ts=time.time() + 300, accepting_orders=True)


class _FakeActiveMarkets:
    def __init__(self, active: set[str] = frozenset()):
        self._active = set(active)

    def active_markets(self) -> set[str]:
        return self._active


def _bot_with_registry(bot_cls, monkeypatch, eligible, *, static_markets=(),
                       orders_active=(), portfolio_active=(), pre_registered=()):
    bot = bot_cls.__new__(bot_cls)
    bot.s = SimpleNamespace(trade_assets=("BTC",), trade_durations=("5m",),
                            markets=tuple(static_markets), max_discovery_markets=6)
    bot.log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                              error=lambda *a, **k: None, debug=lambda *a, **k: None)
    bot.recorder = NullRecorder()
    bot.registry = MarketRegistry(_FakeExchange())
    bot.orders = _FakeActiveMarkets(orders_active)
    bot.portfolio = _FakeActiveMarkets(portfolio_active)
    bot.market_ws = None  # skip the resubscribe branch in this unit test
    bot.user_ws = None
    bot.auth = None
    bot._gamma = object()  # never actually used; discover_eligible_markets is faked below

    async def _fake_discover(gamma, *, assets, durations):
        return eligible

    monkeypatch.setattr(live_mod, "discover_eligible_markets", _fake_discover)
    monkeypatch.setattr(paper_mod, "discover_eligible_markets", _fake_discover)

    for condition_id in pre_registered:
        asyncio.run(bot.registry.register(condition_id))
    return bot


class _TaggingRecorder(NullRecorder):
    def __init__(self):
        self.tags: dict[str, tuple[str, str]] = {}

    def register_market_tags(self, market_id: str, asset: str, duration: str) -> None:
        self.tags[market_id] = (asset, duration)


def test_live_discovery_registers_new_eligible_market(monkeypatch):
    bot = _bot_with_registry(TradingBot, monkeypatch, [_classified("c1")])
    asyncio.run(bot._run_discovery_cycle())
    assert bot.registry.info("c1") is not None
    assert "c1" in {s.market_id for s in bot.registry.all_states()}


def test_paper_discovery_registers_new_eligible_market(monkeypatch):
    bot = _bot_with_registry(PaperTradingBot, monkeypatch, [_classified("c1")])
    bot.exchange = None  # no PaperExchange depth cache needed for this assertion
    asyncio.run(bot._run_discovery_cycle())
    assert bot.registry.info("c1") is not None
    assert "c1" in {s.market_id for s in bot.registry.all_states()}


def test_paper_discovery_caps_registrations_to_soonest(monkeypatch):
    # Windows are listed a day ahead of their actual resolving window, so a
    # single cycle can surface far more eligible markets than the engine
    # should ever hold open positions in at once.
    soon = [_classified(f"soon-{i}", duration="5m") for i in range(3)]
    for i, m in enumerate(soon):
        object.__setattr__(m, "end_ts", time.time() + 60 + i)
    far = [_classified(f"far-{i}", duration="5m") for i in range(10)]
    for i, m in enumerate(far):
        object.__setattr__(m, "end_ts", time.time() + 86400 + i)
    bot = _bot_with_registry(PaperTradingBot, monkeypatch, soon + far)
    bot.s.max_discovery_markets = 3
    bot.exchange = None
    asyncio.run(bot._run_discovery_cycle())
    registered = {s.market_id for s in bot.registry.all_states()}
    assert registered == {m.condition_id for m in soon}


def test_paper_discovery_retires_a_flat_ineligible_market(monkeypatch):
    bot = _bot_with_registry(PaperTradingBot, monkeypatch, [], pre_registered=["c-old"])
    bot.exchange = None
    assert "c-old" in {s.market_id for s in bot.registry.all_states()}
    asyncio.run(bot._run_discovery_cycle())
    assert "c-old" not in {s.market_id for s in bot.registry.all_states()}
    assert bot.registry.is_retired("c-old")


def test_paper_discovery_never_retires_a_market_with_open_orders(monkeypatch):
    bot = _bot_with_registry(PaperTradingBot, monkeypatch, [], pre_registered=["c-busy"],
                             orders_active={"c-busy"})
    bot.exchange = None
    asyncio.run(bot._run_discovery_cycle())
    assert "c-busy" in {s.market_id for s in bot.registry.all_states()}
    assert not bot.registry.is_retired("c-busy")


def test_paper_discovery_never_retires_a_market_with_a_position(monkeypatch):
    bot = _bot_with_registry(PaperTradingBot, monkeypatch, [], pre_registered=["c-held"],
                             portfolio_active={"c-held"})
    bot.exchange = None
    asyncio.run(bot._run_discovery_cycle())
    assert "c-held" in {s.market_id for s in bot.registry.all_states()}


def test_paper_discovery_never_retires_a_statically_configured_market(monkeypatch):
    bot = _bot_with_registry(PaperTradingBot, monkeypatch, [], static_markets=["c-static"],
                             pre_registered=["c-static"])
    bot.exchange = None
    asyncio.run(bot._run_discovery_cycle())
    assert "c-static" in {s.market_id for s in bot.registry.all_states()}


def test_paper_discovery_leaves_already_active_market_alone(monkeypatch):
    """Re-discovering a market that's already registered must not re-register it."""
    bot = _bot_with_registry(PaperTradingBot, monkeypatch, [_classified("c1")],
                             pre_registered=["c1"])
    bot.exchange = None
    exchange = bot.registry._exchange
    asyncio.run(bot._run_discovery_cycle())
    assert exchange.registered.count("c1") == 1  # only the pre-registration call


def test_paper_discovery_stamps_asset_duration_tags(monkeypatch):
    bot = _bot_with_registry(PaperTradingBot, monkeypatch,
                             [_classified("c1", asset="ETH", duration="15m")])
    bot.exchange = None
    bot.recorder = _TaggingRecorder()
    asyncio.run(bot._run_discovery_cycle())
    assert bot.recorder.tags["c1"] == ("ETH", "15m")


# ======================================================================
# Backward-compatibility: discovery loop exits immediately when disabled
# ======================================================================
def test_live_discovery_loop_exits_immediately_when_disabled(monkeypatch):
    """_market_discovery_loop must return without blocking when trade_assets/
    trade_durations are both unset — preserving the pre-discovery boot path."""
    bot = TradingBot.__new__(TradingBot)
    bot.s = SimpleNamespace(trade_assets=(), trade_durations=())
    bot._shutdown = asyncio.Event()

    async def _run():
        await asyncio.wait_for(bot._market_discovery_loop(), timeout=1.0)

    asyncio.run(_run())  # would hang if the loop didn't early-exit


def test_paper_discovery_loop_exits_immediately_when_disabled(monkeypatch):
    """Same backward-compat guarantee for the paper engine."""
    bot = PaperTradingBot.__new__(PaperTradingBot)
    bot.s = SimpleNamespace(trade_assets=(), trade_durations=())
    bot._shutdown = asyncio.Event()

    async def _run():
        await asyncio.wait_for(bot._market_discovery_loop(), timeout=1.0)

    asyncio.run(_run())  # would hang if the loop didn't early-exit


def test_discovery_retires_a_flat_ineligible_market(monkeypatch):
    bot = _bot_with_registry(TradingBot, monkeypatch, [], pre_registered=["c-old"])
    assert "c-old" in {s.market_id for s in bot.registry.all_states()}
    asyncio.run(bot._run_discovery_cycle())
    assert "c-old" not in {s.market_id for s in bot.registry.all_states()}
    assert bot.registry.is_retired("c-old")


def test_discovery_never_retires_a_market_with_open_orders(monkeypatch):
    bot = _bot_with_registry(TradingBot, monkeypatch, [], pre_registered=["c-busy"],
                             orders_active={"c-busy"})
    asyncio.run(bot._run_discovery_cycle())
    assert "c-busy" in {s.market_id for s in bot.registry.all_states()}
    assert not bot.registry.is_retired("c-busy")


def test_discovery_never_retires_a_market_with_a_position(monkeypatch):
    bot = _bot_with_registry(TradingBot, monkeypatch, [], pre_registered=["c-held"],
                             portfolio_active={"c-held"})
    asyncio.run(bot._run_discovery_cycle())
    assert "c-held" in {s.market_id for s in bot.registry.all_states()}


def test_discovery_never_retires_a_statically_configured_market(monkeypatch):
    bot = _bot_with_registry(TradingBot, monkeypatch, [], static_markets=["c-static"],
                             pre_registered=["c-static"])
    asyncio.run(bot._run_discovery_cycle())
    assert "c-static" in {s.market_id for s in bot.registry.all_states()}


def test_discovery_leaves_already_active_market_alone(monkeypatch):
    """Re-discovering a market that's already registered must not re-register it
    (which would reset its running MarketState)."""
    bot = _bot_with_registry(TradingBot, monkeypatch, [_classified("c1")], pre_registered=["c1"])
    exchange = bot.registry._exchange
    asyncio.run(bot._run_discovery_cycle())
    assert exchange.registered.count("c1") == 1  # only the pre-registration call


def test_discovery_stamps_asset_duration_tags(monkeypatch):
    bot = _bot_with_registry(TradingBot, monkeypatch, [_classified("c1", asset="ETH", duration="15m")])
    bot.recorder = _TaggingRecorder()
    asyncio.run(bot._run_discovery_cycle())
    assert bot.recorder.tags["c1"] == ("ETH", "15m")
