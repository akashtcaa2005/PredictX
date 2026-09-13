"""Periodic publisher that snapshots the running engine into telemetry.

The order manager records *events* as they happen (orders, fills). This task
records the engine's *state*: marked positions, an equity sample, per-strategy
status and a full heartbeat document. It is the sole thing the dashboard needs
in order to render a live engine — and because it lands in SQLite, the last known
state survives the engine being stopped or restarted.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from polybot.utils.time_utils import now_s, utcnow_iso


class TelemetryPublisher:
    """Samples engine state on a fixed cadence and hands it to the recorder."""

    def __init__(self, *, recorder: Any, registry: Any, portfolio: Any, orders: Any, risk: Any,
                 strategy: Any, runner: Any, metrics: Any, sockets: list[Any], mode: str,
                 cash_provider: Callable[[], float], wallet_address: str = "",
                 funder_address: str = "", interval_s: float = 2.0,
                 equity_interval_s: float = 15.0, logger: logging.Logger | None = None) -> None:
        self._rec = recorder
        self._registry = registry
        self._portfolio = portfolio
        self._orders = orders
        self._risk = risk
        self._strategy = strategy
        self._runner = runner
        self._metrics = metrics
        self._sockets = sockets
        self._mode = mode
        self._cash = cash_provider
        self._wallet = wallet_address
        self._funder = funder_address
        self._interval = interval_s
        self._equity_interval = equity_interval_s
        self._log = logger or logging.getLogger("polybot.telemetry")
        self._stop = asyncio.Event()
        self._last_equity_ts = 0.0
        self._started_ts = now_s()
        self._last_signal: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    def set_sockets(self, sockets: list[Any]) -> None:
        """Point at a new set of websocket objects (e.g. after a rotation-driven
        reconnect), so status reporting doesn't freeze on a dead socket."""
        self._sockets = sockets

    def note_signal(self, market_id: str, signal: str) -> None:
        """Record the most recent strategy action for a market (BUY/SELL/CANCEL)."""
        self._last_signal[market_id] = (signal, now_s())

    # ------------------------------------------------------------------
    async def run(self) -> None:
        self._log.info("telemetry publisher started", extra={"mode": self._mode,
                                                             "interval_s": self._interval})
        while not self._stop.is_set():
            try:
                self._publish()
            except Exception as exc:  # noqa: BLE001 - telemetry must never kill the engine
                self._log.error("telemetry publish failed", extra={"error": repr(exc)}, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def mark_prices(self) -> dict[str, float]:
        """Best available mark (mid, else bid, else ask) per outcome token."""
        marks: dict[str, float] = {}
        for state in self._registry.all_states():
            bar = state.latest
            if bar is None:
                continue
            for side, bid, ask in (("up", bar.up_bid, bar.up_ask), ("down", bar.down_bid, bar.down_ask)):
                if bid > 0 and ask > 0:
                    mark = (bid + ask) / 2.0
                elif bid > 0:
                    mark = bid
                elif ask > 0:
                    mark = ask
                else:
                    continue
                marks[state.token_id(side)] = round(mark, 6)
        return marks

    def _publish(self) -> None:
        marks = self.mark_prices()
        pnl = self._portfolio.snapshot(marks)
        cash = float(self._cash())
        positions_value = 0.0
        rows: list[dict[str, Any]] = []
        for row in pnl["positions"]:
            state = self._registry.state(row["market"])
            token_id = state.token_id(row["side"]) if state else ""
            mark = marks.get(token_id, row["avg_price"])
            positions_value += row["qty"] * mark
            rows.append({
                "market_id": row["market"], "side": row["side"],
                "market_label": _label(self._registry, row["market"]),
                "token_id": token_id, "qty": row["qty"], "avg_price": row["avg_price"],
                "realized_pnl": 0.0, "mark_price": mark, "unrealized": row["unrealized"],
                "ts_updated": now_s(),
            })
        # Positions that closed since the last sample must be zeroed, not left stale.
        for state in self._registry.all_states():
            for side in ("up", "down"):
                if not any(r["market_id"] == state.market_id and r["side"] == side for r in rows):
                    rows.append({
                        "market_id": state.market_id, "side": side,
                        "market_label": _label(self._registry, state.market_id),
                        "token_id": state.token_id(side), "qty": 0.0, "avg_price": 0.0,
                        "realized_pnl": 0.0, "mark_price": marks.get(state.token_id(side), 0.0),
                        "unrealized": 0.0, "ts_updated": now_s(),
                    })
        self._rec.record_positions(rows)
        self._rec.record_strategy_state(self._strategy_rows(pnl, marks))

        snapshot = self._snapshot(pnl, cash, positions_value, marks)
        self._rec.record_heartbeat(snapshot)

        if now_s() - self._last_equity_ts >= self._equity_interval:
            self._last_equity_ts = now_s()
            self._rec.record_equity({
                "balance": round(cash, 6),
                "cash": round(cash, 6),
                "positions_value": round(positions_value, 6),
                "equity": round(cash + positions_value, 6),
                "realized_pnl": pnl["realized_pnl"],
                "unrealized_pnl": pnl["unrealized_pnl"],
                "exposure": pnl["exposure"],
                "open_orders": self._orders.open_count(),
                "open_positions": pnl["open_positions"],
            })

    def _snapshot(self, pnl: dict[str, Any], cash: float, positions_value: float,
                  marks: dict[str, float]) -> dict[str, Any]:
        return {
            "ts": utcnow_iso(),
            "ts_epoch": now_s(),
            "mode": self._mode,
            "uptime_s": round(now_s() - self._started_ts, 1),
            "wallet": self._wallet,
            "funder": self._funder,
            "cash": round(cash, 6),
            "positions_value": round(positions_value, 6),
            "equity": round(cash + positions_value, 6),
            "orders": self._orders.snapshot(),
            "pnl": {"realized": pnl["realized_pnl"], "unrealized": pnl["unrealized_pnl"],
                    "total": pnl["total_pnl"]},
            "exposure": pnl["exposure"],
            "open_positions": pnl["open_positions"],
            "risk": self._risk.status(),
            "connections": [s.status for s in self._sockets],
            "strategy": {"name": getattr(self._strategy, "name", "?"),
                         "ticks": getattr(self._runner, "ticks", 0),
                         "last_tick_ts": getattr(self._runner, "last_tick_ts", 0.0)},
            "markets": [self._market_row(state, marks) for state in self._registry.all_states()],
            "metrics": self._metrics.snapshot() if self._metrics else {},
        }

    def _market_row(self, state: Any, marks: dict[str, float]) -> dict[str, Any]:
        bar = state.latest
        info = self._registry.info(state.market_id)
        row: dict[str, Any] = {
            "market_id": state.market_id,
            "label": getattr(info, "up_outcome", "") and _label(self._registry, state.market_id),
            "tick_size": state.tick_size,
            "min_order_size": state.min_order_size,
            "neg_risk": state.neg_risk,
            "time_remaining": None if state.time_remaining == float("inf") else round(state.time_remaining, 1),
            "close_ts": state.close_ts,
            "accepting_orders": getattr(info, "accepting_orders", True),
            "last_update": bar.timestamp if bar else None,
        }
        for side in ("up", "down"):
            token = state.token_id(side)
            bid = getattr(bar, f"{side}_bid", 0.0) if bar else 0.0
            ask = getattr(bar, f"{side}_ask", 0.0) if bar else 0.0
            row[side] = {
                "token_id": token, "bid": bid, "ask": ask,
                "mid": marks.get(token, 0.0),
                "spread": round(ask - bid, 6) if (bid > 0 and ask > 0) else None,
            }
        return row

    def _strategy_rows(self, pnl: dict[str, Any], marks: dict[str, float]) -> list[dict[str, Any]]:
        """One row per (strategy, market).

        Signal and confidence are read from the strategy object when it exposes
        them (``signal_for``/``confidence_for``/``signals``); the compiled strategy
        does not, so those fall back to the last broker action we observed and
        ``None`` respectively — never a fabricated number.
        """
        name = getattr(self._strategy, "name", "strategy")
        running = 1 if getattr(self._runner, "ticks", 0) > 0 else 0
        rows = []
        for state in self._registry.all_states():
            market_id = state.market_id
            signal, signal_ts = self._last_signal.get(market_id, (None, None))
            pos = next((p for p in pnl["positions"] if p["market"] == market_id), None)
            rows.append({
                "strategy": name,
                "market_id": market_id,
                "market_label": _label(self._registry, market_id),
                "ts": now_s(),
                "enabled": 1,
                "running": running,
                "last_signal": signal,
                "last_signal_ts": signal_ts,
                "current_signal": _probe(self._strategy, "signal_for", market_id) or signal,
                "confidence": _probe(self._strategy, "confidence_for", market_id),
                "position_qty": pos["qty"] if pos else 0.0,
                "entry_price": pos["avg_price"] if pos else None,
                "exit_price": _probe(self._strategy, "exit_price_for", market_id),
                "stop_loss": _probe(self._strategy, "stop_loss_for", market_id),
                "take_profit": _probe(self._strategy, "take_profit_for", market_id),
                "unrealized": pos["unrealized"] if pos else 0.0,
                "ticks": getattr(self._runner, "ticks", 0),
                "errors": int(self._metrics.counter("strategy.errors")) if self._metrics else 0,
            })
        return rows


def _probe(strategy: Any, method: str, market_id: str) -> Any:
    """Call an optional strategy introspection hook; ``None`` when unsupported."""
    fn = getattr(strategy, method, None)
    if not callable(fn):
        return None
    try:
        return fn(market_id)
    except Exception:  # noqa: BLE001 - introspection is strictly best-effort
        return None


def _label(registry: Any, market_id: str) -> str | None:
    info = registry.info(market_id)
    if info is None:
        return None
    return getattr(info, "label", None) or f"{info.up_outcome}/{info.down_outcome}"
