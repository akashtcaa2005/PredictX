"""Offline tests for :class:`polybot.wallet.allowance.AllowanceManager`.

No network: a fake ClobExchange returns canned allowance maps so we can assert
the check/skip/block logic across dry-run and live, sufficient and insufficient
allowances, and the wrong-network guard.
"""
from __future__ import annotations

import asyncio

from polybot.wallet.allowance import AllowanceError, AllowanceManager

CTF = "0xE111180000d2663C0091e4f400237545B87B996B"
NRE = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NRA = "0xe2222d279d744050d28e00520010520000310F59"


class FakeExchange:
    """Stand-in for ClobExchange exposing only the allowance methods used here."""

    def __init__(self, allowances: dict[str, float], *, raise_on_get: bool = False) -> None:
        self._allowances = allowances
        self._raise = raise_on_get
        self.refreshed = 0

    async def refresh_collateral_allowance(self):
        self.refreshed += 1
        return {"success": True}

    async def get_collateral_allowances(self) -> dict[str, float]:
        if self._raise:
            raise RuntimeError("clob unreachable")
        return dict(self._allowances)


def _mgr(exchange, *, live: bool, chain_id: int = 137, min_allowance: float = 100.0):
    return AllowanceManager(exchange, signature_type=1, chain_id=chain_id,
                            min_allowance=min_allowance, enable_live_trading=live,
                            funder="0xFUNDER")


def test_sufficient_allowance_passes_and_refreshes():
    ex = FakeExchange({CTF: 1e12, NRE: 1e12, NRA: 1e12})
    assert asyncio.run(_mgr(ex, live=True).verify()) is True
    assert ex.refreshed == 1  # backend cache was refreshed before reading


def test_insufficient_blocks_live():
    ex = FakeExchange({CTF: 0.0, NRE: 0.0, NRA: 0.0})
    try:
        asyncio.run(_mgr(ex, live=True).verify())
    except AllowanceError:
        pass
    else:
        raise AssertionError("expected AllowanceError in live mode with 0 allowance")


def test_insufficient_warns_in_dry_run():
    ex = FakeExchange({CTF: 0.0, NRE: 0.0, NRA: 0.0})
    # dry-run must not raise; returns False so the bot keeps running (simulated).
    assert asyncio.run(_mgr(ex, live=False).verify()) is False


def test_partial_shortfall_blocks_live():
    ex = FakeExchange({CTF: 1e12, NRE: 1e12, NRA: 5.0})  # one spender under the floor
    try:
        asyncio.run(_mgr(ex, live=True, min_allowance=100.0).verify())
    except AllowanceError:
        pass
    else:
        raise AssertionError("expected AllowanceError when any spender is under-approved")


def test_wrong_network_blocks_live():
    ex = FakeExchange({CTF: 1e12, NRE: 1e12, NRA: 1e12})
    try:
        asyncio.run(_mgr(ex, live=True, chain_id=1).verify())  # not Polygon
    except AllowanceError:
        pass
    else:
        raise AssertionError("expected AllowanceError on non-Polygon chain")
    # and it must not even read allowances on the wrong network
    assert ex.refreshed == 0


def test_empty_allowance_map_blocks_live():
    ex = FakeExchange({})
    try:
        asyncio.run(_mgr(ex, live=True).verify())
    except AllowanceError:
        pass
    else:
        raise AssertionError("expected AllowanceError when allowance data is unavailable")


def test_read_error_blocks_live_but_not_dry_run():
    ex = FakeExchange({}, raise_on_get=True)
    try:
        asyncio.run(_mgr(ex, live=True).verify())
    except AllowanceError:
        pass
    else:
        raise AssertionError("expected AllowanceError when allowance read fails in live")
    ex2 = FakeExchange({}, raise_on_get=True)
    assert asyncio.run(_mgr(ex2, live=False).verify()) is False


def _run_all():
    test_sufficient_allowance_passes_and_refreshes()
    test_insufficient_blocks_live()
    test_insufficient_warns_in_dry_run()
    test_partial_shortfall_blocks_live()
    test_wrong_network_blocks_live()
    test_empty_allowance_map_blocks_live()
    test_read_error_blocks_live_but_not_dry_run()


if __name__ == "__main__":
    _run_all()
    print("allowance manager (offline): ALL PASSED ✅")
