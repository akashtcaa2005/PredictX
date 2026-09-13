"""Risk engine — the single authority on whether an order may be submitted.

Exposes two surfaces:

* :meth:`notional_ok` — the small check the *compiled strategy* calls before
  committing notional to a market.
* :meth:`check_order` — the full pre-trade gate the :class:`OrderManager` runs on
  every intent (max order value, max position size, max exposure, max concurrent
  trades, max open orders, cooldown, daily-loss, wallet balance), plus the global
  halts: an emergency stop and a filesystem **kill switch**.

All checks are synchronous so they can run inside the strategy worker thread.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from polybot.config.settings import Settings
from polybot.orders.models import Action, OrderIntent
from polybot.utils.time_utils import now_s


@dataclass
class RiskDecision:
    """Outcome of a risk check."""

    ok: bool
    reason: str = "ok"


class RiskEngine:
    """Enforces trading limits and global stops."""

    def __init__(self, settings: Settings, *, portfolio: Any, balance: Any,
                 logger: logging.Logger | None = None, metrics: Any = None) -> None:
        self._s = settings
        self._portfolio = portfolio
        self._balance = balance
        self._orders: Any = None  # bound after construction (circular dependency)
        self._log = logger or logging.getLogger("polybot.risk")
        self._metrics = metrics
        self._lock = threading.Lock()

        self._emergency_stop = False
        self._halt_reason = ""
        self._kill_switch_file = Path(settings.kill_switch_file)
        self._last_activity_ts: dict[str, float] = {}
        self._day = date.today()
        self._realized_pnl_today = 0.0

    def bind_orders(self, orders: Any) -> None:
        """Late-bind the OrderManager (needed for open-order counts)."""
        self._orders = orders

    # -- strategy-facing check -------------------------------------------------
    def notional_ok(self, current_notional: float, limit: float) -> bool:
        """Called by the strategy: is there room under ``limit`` (and not halted)?"""
        if self.is_halted():
            return False
        return current_notional < limit

    # -- global stops ----------------------------------------------------------
    def is_halted(self) -> bool:
        if self._emergency_stop:
            return True
        if self._kill_switch_file.exists():
            if not self._halt_reason:
                self.emergency_stop(f"kill switch file present: {self._kill_switch_file}")
            return True
        return False

    def emergency_stop(self, reason: str) -> None:
        with self._lock:
            if not self._emergency_stop:
                self._emergency_stop = True
                self._halt_reason = reason
        self._log.critical("EMERGENCY STOP engaged", extra={"reason": reason})
        if self._metrics:
            self._metrics.incr("risk.emergency_stop")

    def resume(self) -> None:
        with self._lock:
            self._emergency_stop = False
            self._halt_reason = ""
        self._log.warning("risk halt cleared; trading resumed")

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    # -- full pre-trade gate ---------------------------------------------------
    def check_order(self, intent: OrderIntent) -> RiskDecision:
        """Validate one order intent against all configured limits."""
        self._maybe_roll_day()

        if self.is_halted():
            return self._reject(intent, f"halted ({self._halt_reason})")

        # daily loss
        loss_today = max(0.0, -self._realized_pnl_today)
        if loss_today >= self._s.max_daily_loss:
            self.emergency_stop(f"daily loss {loss_today:.2f} >= limit {self._s.max_daily_loss:.2f}")
            return self._reject(intent, "max daily loss reached")

        # per-order value
        if intent.notional > self._s.max_order_value + 1e-9:
            return self._reject(intent, f"order value {intent.notional:.2f} > max {self._s.max_order_value:.2f}")

        # open-order ceiling
        if self._orders is not None and self._orders.open_count() >= self._s.max_open_orders:
            return self._reject(intent, f"open orders >= max {self._s.max_open_orders}")

        # concurrent-trade ceiling (distinct markets with activity)
        active = set(self._portfolio.active_markets())
        if self._orders is not None:
            active |= set(self._orders.active_markets())
        if intent.market_id not in active and len(active) >= self._s.max_concurrent_trades:
            return self._reject(intent, f"concurrent markets >= max {self._s.max_concurrent_trades}")

        # cooldown after activity in this market
        if self._s.cooldown_seconds > 0:
            last = self._last_activity_ts.get(intent.market_id)
            if last is not None and (now_s() - last) < self._s.cooldown_seconds:
                return self._reject(intent, "cooldown active")

        if intent.action == Action.BUY:
            # position-size ceiling
            projected = self._portfolio.position_qty(intent.market_id, intent.strategy_side) + intent.size
            if projected > self._s.max_position_size + 1e-9:
                return self._reject(intent, f"position size {projected:.0f} > max {self._s.max_position_size:.0f}")
            # total-exposure ceiling
            projected_exposure = self._portfolio.total_exposure() + intent.notional
            if self._orders is not None:
                projected_exposure += self._orders.open_notional_total()
            if projected_exposure > self._s.max_total_exposure + 1e-9:
                return self._reject(intent, f"exposure {projected_exposure:.2f} > max {self._s.max_total_exposure:.2f}")
            # wallet balance
            if not self._balance.has_funds_for(intent.notional):
                return self._reject(intent, "insufficient wallet balance")

        return RiskDecision(True)

    def record_submission(self, intent: OrderIntent) -> None:
        """Mark market activity (drives the cooldown timer)."""
        self._last_activity_ts[intent.market_id] = now_s()

    def record_fill(self, realized_pnl_delta: float) -> None:
        """Fold a realized PnL delta into the daily total; halt if the limit trips."""
        with self._lock:
            self._realized_pnl_today += realized_pnl_delta
            loss = max(0.0, -self._realized_pnl_today)
        if loss >= self._s.max_daily_loss:
            self.emergency_stop(f"daily loss {loss:.2f} >= limit {self._s.max_daily_loss:.2f}")

    def _maybe_roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._log.info("new trading day; resetting daily counters",
                          extra={"realized_pnl_prev": round(self._realized_pnl_today, 4)})
            self._day = today
            self._realized_pnl_today = 0.0

    def _reject(self, intent: OrderIntent, reason: str) -> RiskDecision:
        if self._metrics:
            self._metrics.incr("risk.rejected")
        self._log.warning("order rejected by risk", extra={"market": intent.market_id,
                                                           "side": intent.strategy_side,
                                                           "action": intent.action.value,
                                                           "price": intent.price, "size": intent.size,
                                                           "reason": reason})
        return RiskDecision(False, reason)

    def status(self) -> dict[str, Any]:
        return {
            "halted": self.is_halted(),
            "halt_reason": self._halt_reason,
            "realized_pnl_today": round(self._realized_pnl_today, 4),
            "daily_loss_limit": self._s.max_daily_loss,
        }
