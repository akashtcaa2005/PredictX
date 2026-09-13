"""Offline regression tests for the dashboard's live-sync CLOB concurrency fix.

dashboard/live_sync.py's sync_once() fires _sync_health, _sync_balance,
_sync_orders and _sync_trades concurrently via asyncio.gather. Each of those
ultimately dispatches a synchronous py-clob-client-v2 call onto a worker
thread, and all of them share ONE module-level httpx.Client — firing several
at once raced that shared client and produced "[Errno 11] Resource
temporarily unavailable" ~100-150 times/hour in production.

The fix adds an asyncio.Lock (self._clob_lock) around just the CLOB-touching
sections, so gather still runs them concurrently at the task level but at
most one is ever mid-flight on the actual (simulated) network call. These
tests use a fake exchange that tracks concurrent-call overlap directly,
rather than trying to reproduce the real socket-level race.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from dashboard.live_sync import LiveAccountSync, _empty_state
from polybot.monitoring.metrics import Metrics


class _CountingExchange:
    """Fake ReadOnlyExchange stand-in that records concurrent-call overlap."""

    def __init__(self, *, fail_orders: bool = False) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self._fail_orders = fail_orders

    async def _tick(self, name: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append(name)
        await asyncio.sleep(0.03)  # simulate a blocking network call
        self.active -= 1

    async def health(self) -> bool:
        await self._tick("health")
        return True

    async def server_time(self) -> int:
        await self._tick("server_time")
        return 12345

    async def get_collateral_balance(self) -> float:
        await self._tick("get_collateral_balance")
        return 250.5

    async def get_collateral_allowances(self) -> dict:
        await self._tick("get_collateral_allowances")
        return {"CTF Exchange": 1000.0}

    async def get_orders(self) -> list:
        await self._tick("get_orders")
        if self._fail_orders:
            raise RuntimeError("[Errno 11] Resource temporarily unavailable")
        return [{"id": "o1", "size": "10", "size_matched": "0", "price": "0.5",
                 "side": "BUY", "status": "LIVE", "created_at": 1.0}]

    async def get_trades(self) -> list:
        await self._tick("get_trades")
        return [{"id": "t1", "size": "5", "price": "0.4", "side": "BUY",
                 "match_time": 1.0, "fee": "0.01"}]


def _make_sync(exchange: _CountingExchange) -> LiveAccountSync:
    """Build a LiveAccountSync without the heavy __init__ (no env file needed)."""
    sync = LiveAccountSync.__new__(LiveAccountSync)
    sync._s = SimpleNamespace(live_slow_sync_interval_s=999_999.0, live_sync_enabled=True)
    sync._clob_lock = asyncio.Lock()
    sync._metrics = Metrics()
    sync._exchange = exchange
    sync._data_api = None
    sync._user_ws = None
    sync.connected = True
    sync.auth_status = "authenticated"
    sync.last_error = None
    sync.last_sync_ts = 0.0
    sync.last_slow_sync_ts = 0.0
    sync.sync_count = 0
    sync.error_count = 0
    sync.state = _empty_state()
    return sync


def test_sync_once_never_overlaps_clob_calls():
    exchange = _CountingExchange()
    sync = _make_sync(exchange)
    asyncio.run(sync.sync_once())
    assert exchange.max_active == 1, "two CLOB calls were in flight at once"
    assert sorted(exchange.calls) == sorted(
        ["health", "server_time", "get_collateral_balance",
         "get_collateral_allowances", "get_orders", "get_trades"])


def test_multiple_sync_cycles_stay_serialized():
    exchange = _CountingExchange()
    sync = _make_sync(exchange)
    # Simulate overlapping cycles (e.g. a slow round still running when the
    # next timer fires) by launching two sync_once() calls concurrently.
    async def _run_both() -> None:
        await asyncio.gather(sync.sync_once(), sync.sync_once())

    asyncio.run(_run_both())
    assert exchange.max_active == 1
    assert sync.sync_count == 2


def test_sync_once_updates_balance_orders_trades_health():
    exchange = _CountingExchange()
    sync = _make_sync(exchange)
    asyncio.run(sync.sync_once())
    assert sync.state["status"]["api_ok"] is True
    assert sync.state["wallet"]["balance"] == 250.5
    assert len(sync.state["open_orders"]) == 1
    assert len(sync.state["trade_history"]) == 1


def test_sync_once_does_not_hide_section_failures():
    exchange = _CountingExchange(fail_orders=True)
    sync = _make_sync(exchange)
    asyncio.run(sync.sync_once())
    assert sync.error_count == 1
    assert "Errno 11" in (sync.last_error or "")
    # the failing section's data stays at its prior (empty) value...
    assert sync.state["open_orders"] == []
    # ...but sections that succeeded still update normally.
    assert sync.state["wallet"]["balance"] == 250.5
    assert len(sync.state["trade_history"]) == 1
