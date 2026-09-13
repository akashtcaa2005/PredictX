"""Health monitoring — periodic snapshot of the bot's vitals.

Reports wallet address, collateral balance, open orders, open positions, PnL,
websocket connection status, latency and a heartbeat timestamp. The heartbeat
loop logs this to ``execution.log`` on a fixed cadence and updates metrics.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from polybot.utils.time_utils import now_s, utcnow_iso


class HealthMonitor:
    """Aggregates a health snapshot and emits a periodic heartbeat."""

    def __init__(self, *, address: str, balance: Any, portfolio: Any, orders: Any, risk: Any,
                 sockets: list[Any], strategy_runner: Any, metrics: Any,
                 interval_s: float = 15.0, status_file: Any = None,
                 logger: logging.Logger | None = None) -> None:
        self._address = address
        self._balance = balance
        self._portfolio = portfolio
        self._orders = orders
        self._risk = risk
        self._sockets = sockets
        self._runner = strategy_runner
        self._metrics = metrics
        self._interval = interval_s
        self._status_file = Path(status_file) if status_file else None
        self._log = logger or logging.getLogger("polybot.execution")
        self._stop = asyncio.Event()
        self.last_heartbeat_ts = 0.0

    def snapshot(self) -> dict[str, Any]:
        pnl = self._portfolio.snapshot()
        return {
            "ts": utcnow_iso(),
            "wallet": self._address,
            "balance": round(self._balance.balance, 4),
            "orders": self._orders.snapshot(),
            "positions": pnl["open_positions"],
            "pnl": {"realized": pnl["realized_pnl"], "unrealized": pnl["unrealized_pnl"],
                    "total": pnl["total_pnl"]},
            "exposure": pnl["exposure"],
            "risk": self._risk.status(),
            "connections": [s.status for s in self._sockets],
            "ticks": self._runner.ticks,
            "latency": self._metrics.latency_stats("clob.latency"),
            "heartbeat_age_s": round(now_s() - self.last_heartbeat_ts, 1) if self.last_heartbeat_ts else None,
        }

    async def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._balance.refresh()
            except Exception as exc:  # noqa: BLE001 - health must never crash
                self._log.debug("balance refresh in heartbeat failed", extra={"error": repr(exc)})
            self.last_heartbeat_ts = now_s()
            snap = self.snapshot()
            # DEBUG, not INFO: the full snapshot is already published atomically to
            # status.json and to the metrics gauges below, so an INFO line every
            # ~15s (≈5,760/day/engine) is pure log-fill. Raise LOG_LEVEL=DEBUG only
            # when diagnosing.
            self._log.debug("HEARTBEAT", extra={"health": snap})
            self._write_status(snap)
            self._metrics.gauge("balance", snap["balance"])
            self._metrics.gauge("open_orders", snap["orders"]["open"])
            self._metrics.gauge("open_positions", snap["positions"])
            self._metrics.gauge("total_pnl", snap["pnl"]["total"])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def _write_status(self, snap: dict[str, Any]) -> None:
        """Publish the latest health snapshot to ``status.json`` (atomic write).

        A machine-readable monitoring feed for external dashboards/probes; uses
        real runtime data only. Never raises — monitoring must not crash the bot.
        """
        if self._status_file is None:
            return
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._status_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap, default=str))
            os.replace(tmp, self._status_file)
        except Exception as exc:  # noqa: BLE001 - monitoring is best-effort
            self._log.debug("status.json write failed", extra={"error": repr(exc)})

    def stop(self) -> None:
        self._stop.set()
