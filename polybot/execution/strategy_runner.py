"""Strategy runner — ticks the compiled strategy against live market state.

Once per ``tick_interval_s`` it feeds every tracked :class:`MarketState` to the
strategy's ``on_bar``. ``on_bar`` is synchronous compiled code, so each tick runs
in a worker thread (:func:`asyncio.to_thread`); the broker calls it makes are
marshalled back onto the event loop by the :class:`OrderManager`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from polybot.exchange.market_registry import MarketRegistry


class StrategyRunner:
    """Drives ``strategy.on_bar`` for all registered markets on a fixed cadence."""

    def __init__(self, strategy: Any, registry: MarketRegistry, *, tick_interval_s: float = 1.0,
                 logger: logging.Logger | None = None, metrics: Any = None) -> None:
        self._strategy = strategy
        self._registry = registry
        self._interval = tick_interval_s
        self._log = logger or logging.getLogger("polybot.strategy")
        self._metrics = metrics
        self._stop = asyncio.Event()
        self.ticks = 0
        self.last_tick_ts = 0.0

    async def run(self) -> None:
        self._log.info("strategy runner started", extra={"strategy": getattr(self._strategy, "name", "?"),
                       "interval_s": self._interval})
        while not self._stop.is_set():
            for state in self._registry.all_states():
                await asyncio.to_thread(self._tick, state)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def _tick(self, state: Any) -> None:
        try:
            self._strategy.on_bar(state)
        except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the loop
            self._log.error("strategy on_bar error", extra={"market": state.market_id,
                            "error": repr(exc)}, exc_info=True)
            if self._metrics:
                self._metrics.incr("strategy.errors")
            return
        self.ticks += 1
        self.last_tick_ts = state.latest.timestamp if state.latest else self.last_tick_ts
        if self._metrics:
            self._metrics.incr("strategy.ticks")

    def stop(self) -> None:
        self._stop.set()
