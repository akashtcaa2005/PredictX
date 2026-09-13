"""Virtual cash ledger for paper trading.

Mirrors what a real collateral account does: cash is debited when a buy fills,
credited when a sell fills, and *reserved* while a buy order rests on the book so
the strategy cannot commit the same dollar twice. The ledger is persisted to the
paper state directory so a restart resumes from the balance it left off at.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from polybot.utils.time_utils import now_s


class PaperAccount:
    """Thread-safe virtual USDC account."""

    def __init__(self, starting_cash: float, *, state_file: Path | None = None,
                 logger: logging.Logger | None = None) -> None:
        self._lock = threading.RLock()
        self._starting_cash = float(starting_cash)
        self._cash = float(starting_cash)
        self._locked = 0.0
        self._fees_paid = 0.0
        self._deposits = float(starting_cash)
        self._state_file = state_file
        self._log = logger or logging.getLogger("polybot.paper.account")
        self._load()

    # -- reads -----------------------------------------------------------------
    @property
    def cash(self) -> float:
        """Total virtual collateral, including what is reserved by open orders."""
        with self._lock:
            return self._cash

    @property
    def locked(self) -> float:
        """Collateral reserved by resting buy orders."""
        with self._lock:
            return self._locked

    @property
    def available(self) -> float:
        with self._lock:
            return max(0.0, self._cash - self._locked)

    @property
    def fees_paid(self) -> float:
        with self._lock:
            return self._fees_paid

    @property
    def starting_cash(self) -> float:
        return self._starting_cash

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "cash": round(self._cash, 6),
                "locked": round(self._locked, 6),
                "available": round(max(0.0, self._cash - self._locked), 6),
                "fees_paid": round(self._fees_paid, 6),
                "starting_cash": round(self._starting_cash, 6),
                "deposits": round(self._deposits, 6),
                "net_pnl": round(self._cash - self._deposits, 6),
            }

    # -- mutations -------------------------------------------------------------
    def reserve(self, amount: float) -> bool:
        """Reserve ``amount`` for a resting buy. False when funds are insufficient."""
        with self._lock:
            if amount <= 0:
                return True
            if self._cash - self._locked < amount:
                return False
            self._locked += amount
            return True

    def release(self, amount: float) -> None:
        """Release a previously reserved amount (cancel / reject / after fill)."""
        with self._lock:
            self._locked = max(0.0, self._locked - max(0.0, amount))

    def debit(self, amount: float, *, fee: float = 0.0) -> None:
        """Settle a buy fill: cash leaves the account."""
        with self._lock:
            self._cash -= (amount + fee)
            self._fees_paid += fee
        self._persist()

    def credit(self, amount: float, *, fee: float = 0.0) -> None:
        """Settle a sell fill: proceeds arrive."""
        with self._lock:
            self._cash += (amount - fee)
            self._fees_paid += fee
        self._persist()

    def reset(self, starting_cash: float | None = None) -> None:
        """Wipe the ledger back to a clean starting balance."""
        with self._lock:
            if starting_cash is not None:
                self._starting_cash = float(starting_cash)
            self._cash = self._starting_cash
            self._locked = 0.0
            self._fees_paid = 0.0
            self._deposits = self._starting_cash
        self._persist()
        self._log.warning("paper account reset", extra={"starting_cash": self._starting_cash})

    # -- persistence -----------------------------------------------------------
    def _load(self) -> None:
        if self._state_file is None or not self._state_file.is_file():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log.warning("could not read paper account state; starting fresh",
                              extra={"error": repr(exc)})
            return
        with self._lock:
            self._cash = float(data.get("cash", self._starting_cash))
            self._fees_paid = float(data.get("fees_paid", 0.0))
            self._deposits = float(data.get("deposits", self._starting_cash))
            self._starting_cash = float(data.get("starting_cash", self._starting_cash))
            self._locked = 0.0  # open orders are re-reserved as they are recreated
        self._log.info("paper account restored", extra={"cash": round(self._cash, 4)})

    def _persist(self) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {**self.snapshot(), "ts": now_s()}
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._state_file)
        except OSError as exc:  # noqa: BLE001 - persistence is best-effort
            self._log.debug("paper account persist failed", extra={"error": repr(exc)})


class PaperBalanceManager:
    """Drop-in replacement for :class:`~polybot.wallet.balance.BalanceManager`."""

    def __init__(self, account: PaperAccount, *, min_balance: float = 0.0,
                 logger: logging.Logger | None = None) -> None:
        self._account = account
        self._min_balance = min_balance
        self._log = logger or logging.getLogger("polybot.paper.balance")

    @property
    def balance(self) -> float:
        return self._account.cash

    async def refresh(self, *, force: bool = False) -> float:
        return self._account.cash

    def has_funds_for(self, notional: float) -> bool:
        return (self._account.available - notional) >= self._min_balance

    async def validate(self) -> float:
        balance = self._account.cash
        self._log.info("paper balance", extra={"balance": balance,
                                               "available": self._account.available})
        return balance
