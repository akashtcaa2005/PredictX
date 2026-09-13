"""Strategy framework contract — bound to the compiled strategy at import time.

The compiled strategy ``Strategy/s1_cheap_buy.cpython-313.pyc`` was built against a
framework that it imports as a *top-level* module named ``base``::

    from base import Strategy, MAX_TRADE_VALUE, shares_for_notional

That framework module was never shipped with the project, so this file recreates
it *exactly* to the interface the bytecode expects. It contains **no strategy
logic** — only the base class the strategy subclasses, the risk/broker contracts
it calls into, and the two helpers/constants it imports.

The live engine injects concrete ``broker`` and ``risk`` implementations into
each :class:`Strategy` instance; the compiled strategy drives them through the
attributes defined here. Nothing in this module may be renamed without breaking
the compiled bytecode.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, tolerating blanks/garbage."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Constants imported by the compiled strategy -----------------------------
# Maximum notional (in collateral units, e.g. USDC) the strategy commits per
# "trade unit". Read from the environment so it is never hardcoded; the config
# layer loads env.txt into os.environ *before* the strategy module is imported.
MAX_TRADE_VALUE: float = _env_float("MAX_TRADE_VALUE", 5.0)

# Notional used to size an individual resting order. Default = MAX_TRADE_VALUE/3
# so the strategy's documented 1c/2c/3c ladder rests on *both* sides within its
# ``notional_ok(..., MAX_TRADE_VALUE * 2)`` cap (6 orders ≈ 2 x MAX_TRADE_VALUE).
# Raise it toward MAX_TRADE_VALUE for fewer, larger tiers. Tunable via env.
PER_ORDER_NOTIONAL: float = _env_float("PER_ORDER_NOTIONAL", MAX_TRADE_VALUE / 3.0)

# Framework-level minimum shares per order (exchanges also enforce their own).
MIN_ORDER_SHARES: float = _env_float("MIN_ORDER_SHARES", 5.0)


def shares_for_notional(price: float, notional: float | None = None) -> float:
    """Return the share quantity whose cost ≈ ``notional`` at ``price``.

    The compiled strategy calls this as ``shares_for_notional(price)`` for each
    price tier, giving roughly constant dollar exposure per tier. The quantity is
    floored to whole shares and clamped to the framework minimum.
    """
    if price <= 0:
        return 0.0
    notional = PER_ORDER_NOTIONAL if notional is None else notional
    qty = float(int(notional / price))  # floor to whole shares
    return qty if qty >= MIN_ORDER_SHARES else MIN_ORDER_SHARES


@runtime_checkable
class Broker(Protocol):
    """Execution surface the strategy drives (implemented by the live executor)."""

    def place_limit(self, market_id: str, side: str, price: float, qty: float) -> Any: ...
    def sell(self, market_id: str, side: str, price: float, size: float) -> Any: ...
    def position_qty(self, market_id: str, side: str) -> float: ...
    def cancel_all_open(self, market_id: str) -> Any: ...
    def notional_in_market(self, market_id: str) -> float: ...


@runtime_checkable
class Risk(Protocol):
    """Risk surface the strategy consults before committing notional."""

    def notional_ok(self, current_notional: float, limit: float) -> bool: ...


class Strategy:
    """Base class every strategy subclasses.

    The live engine constructs each strategy with a concrete ``broker`` and
    ``risk`` (and an optional ``portfolio`` used for exposure accounting), then
    calls :meth:`on_bar` once per market tick. Per-market scratch state is kept in
    a plain dict returned by :meth:`market_state` that persists across ticks for
    the lifetime of the process.
    """

    #: Human-readable identifier; subclasses override (e.g. ``"1c_buy"``).
    name: str = "base"

    def __init__(self, broker: Broker, risk: Risk, portfolio: Any | None = None) -> None:
        self.broker = broker
        self.risk = risk
        self.portfolio = portfolio
        self._state: dict[str, dict[str, Any]] = {}

    def market_state(self, market_id: str) -> dict[str, Any]:
        """Return the persistent per-market scratch dict (created on first use)."""
        return self._state.setdefault(market_id, {})

    def notional_in_market(self, market_id: str) -> float:
        """Current committed notional (open orders + positions) in ``market_id``."""
        if self.portfolio is not None and hasattr(self.portfolio, "notional_in_market"):
            return float(self.portfolio.notional_in_market(market_id))
        return float(self.broker.notional_in_market(market_id))

    def on_bar(self, market: Any) -> None:  # pragma: no cover - overridden by subclass
        """Handle one market snapshot. Implemented by concrete strategies."""
        raise NotImplementedError
