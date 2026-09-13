"""Wallet balance management and validation."""
from __future__ import annotations

import logging

from polybot.exchange.clob import ClobExchange
from polybot.utils.time_utils import now_s


class InsufficientBalanceError(RuntimeError):
    """Raised when the collateral balance is below the configured minimum."""


class BalanceManager:
    """Tracks the collateral (USDC) balance with a short cache."""

    def __init__(self, exchange: ClobExchange, *, min_balance: float,
                 refresh_interval: float = 15.0, logger: logging.Logger | None = None) -> None:
        self._exchange = exchange
        self._min_balance = min_balance
        self._refresh_interval = refresh_interval
        self._log = logger or logging.getLogger("polybot.wallet")
        self._balance = 0.0
        self._fetched_ts = 0.0

    @property
    def balance(self) -> float:
        """Last known collateral balance (human USDC units)."""
        return self._balance

    async def refresh(self, *, force: bool = False) -> float:
        """Fetch the balance if the cache is stale (or ``force``)."""
        if not force and (now_s() - self._fetched_ts) < self._refresh_interval:
            return self._balance
        self._balance = await self._exchange.get_collateral_balance()
        self._fetched_ts = now_s()
        self._log.debug("balance refreshed", extra={"balance": self._balance})
        return self._balance

    def has_funds_for(self, notional: float) -> bool:
        """Whether the cached balance covers ``notional`` and stays above the floor."""
        return (self._balance - notional) >= self._min_balance

    async def validate(self) -> float:
        """Startup check: ensure the balance meets the minimum. Raises otherwise."""
        balance = await self.refresh(force=True)
        if balance < self._min_balance:
            raise InsufficientBalanceError(
                f"collateral balance {balance:.4f} < required minimum {self._min_balance:.4f}"
            )
        self._log.info("wallet balance validated", extra={"balance": balance,
                                                          "min_required": self._min_balance})
        return balance
