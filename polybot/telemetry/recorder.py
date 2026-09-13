"""Durable recorder that mirrors engine activity into the per-mode database.

The trading engine must never block on disk I/O, so every ``record_*`` call is a
cheap enqueue: a single background writer thread drains the queue, batches the
statements into one transaction and commits. Because that thread is the only
writer, it can also keep the FIFO lot book used to assemble completed round-trip
trades from the raw fill stream.

A dropped telemetry write must never take trading down; the writer swallows and
logs its own errors and keeps going.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque

from polybot.telemetry.schema import init_engine_db
from polybot.utils.time_utils import now_s

_SENTINEL = ("__stop__", None)


class _Lot:
    """One open buy lot: quantity still held and what it cost."""

    __slots__ = ("qty", "price", "ts", "strategy")

    def __init__(self, qty: float, price: float, ts: float, strategy: str) -> None:
        self.qty = qty
        self.price = price
        self.ts = ts
        self.strategy = strategy


class EngineRecorder:
    """Async-safe writer for orders, fills, trades, positions and equity."""

    def __init__(self, db_path: str | Path, *, mode: str, strategy_name: str = "",
                 logger: logging.Logger | None = None, max_queue: int = 20_000) -> None:
        self.db_path = Path(db_path)
        self.mode = mode
        self.strategy_name = strategy_name
        self._log = logger or logging.getLogger("polybot.telemetry")
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self._lots: dict[tuple[str, str], Deque[_Lot]] = defaultdict(deque)
        self._dropped = 0
        self._labels: dict[str, str] = {}
        self._tags: dict[str, tuple[str, str]] = {}

    # ======================================================================
    # Lifecycle
    # ======================================================================
    def start(self) -> None:
        """Open the database, restore lot state and start the writer thread."""
        self._conn = init_engine_db(self.db_path)
        self._restore_lots()
        self._thread = threading.Thread(target=self._run, name=f"telemetry-{self.mode}", daemon=True)
        self._thread.start()
        self._log.info("telemetry recorder started", extra={"db": str(self.db_path), "mode": self.mode})

    def stop(self, *, timeout: float = 5.0) -> None:
        """Drain the queue, flush and close. Safe to call more than once."""
        if self._thread is None:
            return
        try:
            self._q.put_nowait(_SENTINEL)
        except queue.Full:  # pragma: no cover - defensive
            pass
        self._thread.join(timeout=timeout)
        self._thread = None
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def register_market_label(self, market_id: str, label: str) -> None:
        """Attach a human-readable name to a condition id for later rows."""
        if label:
            self._labels[market_id] = label

    def _label(self, market_id: str) -> str | None:
        return self._labels.get(market_id)

    def register_market_tags(self, market_id: str, asset: str, duration: str) -> None:
        """Attach an asset/duration classification (e.g. 'BTC'/'5m') for later rows.

        Must be called before any order for this market is placed, so no
        order/trade row is ever written with a null tag.
        """
        if asset and duration:
            self._tags[market_id] = (asset, duration)

    def _tag(self, market_id: str) -> tuple[str | None, str | None]:
        return self._tags.get(market_id, (None, None))

    # ======================================================================
    # Enqueue surface (callable from any thread / the event loop)
    # ======================================================================
    def _put(self, kind: str, payload: Any) -> None:
        try:
            self._q.put_nowait((kind, payload))
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                self._log.warning("telemetry queue full; dropping records",
                                  extra={"dropped": self._dropped})

    def record_order(self, order: Any, *, exchange_response: Any = None,
                     execution_ms: float | None = None, strategy: str | None = None) -> None:
        """Upsert one order row from a :class:`polybot.orders.models.Order`."""
        i = order.intent
        terminal = getattr(order.status, "is_terminal", False)
        asset, duration = self._tag(i.market_id)
        self._put("order", {
            "client_id": order.client_id,
            "exchange_order_id": order.exchange_order_id,
            "ts_created": order.created_ts,
            "ts_updated": order.updated_ts,
            "ts_submitted": getattr(order, "submitted_ts", None),
            "ts_terminal": order.updated_ts if terminal else None,
            "market_id": i.market_id,
            "market_label": self._label(i.market_id),
            "asset": asset,
            "duration": duration,
            "side": i.strategy_side,
            "token_id": i.token_id,
            "action": i.action.value,
            "order_type": i.order_type,
            "price": i.price,
            "size": i.size,
            "filled_size": order.filled_size,
            "avg_fill_price": order.avg_fill_price,
            "notional": i.price * i.size,
            "fees": getattr(order, "fees", 0.0) or 0.0,
            "status": order.status.value,
            "last_error": order.last_error,
            "exchange_response": _json(exchange_response) if exchange_response is not None else None,
            "execution_ms": execution_ms,
            "simulated": 1 if order.simulated else 0,
            "strategy": strategy or self.strategy_name,
        })

    def record_fill(self, order: Any, *, price: float, size: float, realized_pnl: float = 0.0,
                    fee: float = 0.0, strategy: str | None = None) -> None:
        """Record a fill and fold it into the FIFO lot book."""
        i = order.intent
        asset, duration = self._tag(i.market_id)
        self._put("fill", {
            "ts": now_s(),
            "client_id": order.client_id,
            "exchange_order_id": order.exchange_order_id,
            "market_id": i.market_id,
            "market_label": self._label(i.market_id),
            "asset": asset,
            "duration": duration,
            "side": i.strategy_side,
            "token_id": i.token_id,
            "action": i.action.value,
            "price": float(price),
            "size": float(size),
            "fee": float(fee),
            "realized_pnl": float(realized_pnl),
            "strategy": strategy or self.strategy_name,
        })

    def record_positions(self, rows: list[dict[str, Any]]) -> None:
        """Replace the positions table with the engine's current view."""
        self._put("positions", rows)

    def record_equity(self, sample: dict[str, Any]) -> None:
        """Append one portfolio sample (drives every equity/PnL chart)."""
        sample = {"ts": now_s(), **sample}
        self._put("equity", sample)

    def record_heartbeat(self, snapshot: dict[str, Any]) -> None:
        self._put("state", ("heartbeat", snapshot))

    def record_config(self, redacted: dict[str, Any]) -> None:
        self._put("state", ("config", redacted))

    def record_startup(self, info: dict[str, Any]) -> None:
        self._put("state", ("startup", info))

    def record_strategy_state(self, rows: list[dict[str, Any]]) -> None:
        self._put("strategy", rows)

    def record_event(self, level: str, kind: str, message: str, **data: Any) -> None:
        """Log a notable event (fuels the dashboard's alerting)."""
        self._put("event", {"ts": now_s(), "level": level, "kind": kind,
                            "message": message, "data": _json(data) if data else None})

    # ======================================================================
    # Writer thread
    # ======================================================================
    def _run(self) -> None:
        assert self._conn is not None
        pending = 0
        while True:
            try:
                kind, payload = self._q.get(timeout=0.5)
            except queue.Empty:
                if pending:
                    self._commit()
                    pending = 0
                continue
            if kind == "__stop__":
                break
            try:
                self._apply(kind, payload)
                pending += 1
            except Exception as exc:  # noqa: BLE001 - telemetry must never kill trading
                self._log.error("telemetry write failed", extra={"kind": kind, "error": repr(exc)})
            if pending >= 200:
                self._commit()
                pending = 0
        self._commit()

    def _commit(self) -> None:
        try:
            if self._conn is not None:
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._log.error("telemetry commit failed", extra={"error": repr(exc)})

    def _apply(self, kind: str, payload: Any) -> None:
        conn = self._conn
        assert conn is not None
        if kind == "order":
            cols = ", ".join(payload)
            marks = ", ".join(f":{k}" for k in payload)
            conn.execute(f"INSERT INTO orders ({cols}) VALUES ({marks}) "
                         f"ON CONFLICT(client_id) DO UPDATE SET "
                         + ", ".join(f"{k}=excluded.{k}" for k in payload if k != "client_id"),
                         payload)
        elif kind == "fill":
            row = dict(payload)
            label = row.pop("market_label", None)
            asset = row.pop("asset", None)
            duration = row.pop("duration", None)
            conn.execute(
                "INSERT INTO fills (ts, client_id, exchange_order_id, market_id, side, token_id, "
                "action, price, size, fee, realized_pnl, strategy) VALUES (:ts, :client_id, "
                ":exchange_order_id, :market_id, :side, :token_id, :action, :price, :size, :fee, "
                ":realized_pnl, :strategy)", row)
            self._fold_lot(row, label, asset, duration)
        elif kind == "positions":
            for row in payload:
                conn.execute(
                    "INSERT INTO positions (market_id, side, market_label, token_id, qty, avg_price, "
                    "realized_pnl, mark_price, unrealized, ts_updated) VALUES (:market_id, :side, "
                    ":market_label, :token_id, :qty, :avg_price, :realized_pnl, :mark_price, "
                    ":unrealized, :ts_updated) ON CONFLICT(market_id, side) DO UPDATE SET "
                    "market_label=excluded.market_label, token_id=excluded.token_id, "
                    "qty=excluded.qty, avg_price=excluded.avg_price, "
                    "realized_pnl=excluded.realized_pnl, mark_price=excluded.mark_price, "
                    "unrealized=excluded.unrealized, ts_updated=excluded.ts_updated", row)
        elif kind == "equity":
            conn.execute(
                "INSERT OR REPLACE INTO equity (ts, balance, cash, positions_value, equity, "
                "realized_pnl, unrealized_pnl, exposure, open_orders, open_positions) "
                "VALUES (:ts, :balance, :cash, :positions_value, :equity, :realized_pnl, "
                ":unrealized_pnl, :exposure, :open_orders, :open_positions)",
                {"balance": 0.0, "cash": 0.0, "positions_value": 0.0, "equity": 0.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 0.0, "exposure": 0.0,
                 "open_orders": 0, "open_positions": 0, **payload})
        elif kind == "state":
            key, value = payload
            conn.execute("INSERT INTO engine_state (key, value, ts) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                         (key, _json(value), now_s()))
        elif kind == "strategy":
            for row in payload:
                cols = ", ".join(row)
                marks = ", ".join(f":{k}" for k in row)
                updates = ", ".join(f"{k}=excluded.{k}" for k in row
                                    if k not in ("strategy", "market_id"))
                conn.execute(f"INSERT INTO strategy_state ({cols}) VALUES ({marks}) "
                             f"ON CONFLICT(strategy, market_id) DO UPDATE SET {updates}", row)
        elif kind == "event":
            conn.execute("INSERT INTO events (ts, level, kind, message, data) "
                         "VALUES (:ts, :level, :kind, :message, :data)", payload)

    # ------------------------------------------------------------------
    # Round-trip assembly
    # ------------------------------------------------------------------
    def _restore_lots(self) -> None:
        """Rebuild the open-lot book from the stored positions after a restart."""
        assert self._conn is not None
        try:
            rows = self._conn.execute(
                "SELECT market_id, side, qty, avg_price, ts_updated FROM positions WHERE qty > 0"
            ).fetchall()
        except sqlite3.Error:  # pragma: no cover - fresh database
            return
        for row in rows:
            self._lots[(row["market_id"], row["side"])].append(
                _Lot(float(row["qty"]), float(row["avg_price"]),
                     float(row["ts_updated"] or now_s()), self.strategy_name))

    def _fold_lot(self, fill: dict[str, Any], label: str | None,
                 asset: str | None = None, tag_duration: str | None = None) -> None:
        """Apply a fill to the FIFO book, emitting a ``trades`` row per closed lot."""
        key = (fill["market_id"], fill["side"])
        lots = self._lots[key]
        if fill["action"] == "BUY":
            lots.append(_Lot(fill["size"], fill["price"], fill["ts"], fill["strategy"] or ""))
            return

        remaining = fill["size"]
        exit_price = fill["price"]
        fee_per_share = (fill["fee"] / fill["size"]) if fill["size"] else 0.0
        while remaining > 1e-9 and lots:
            lot = lots[0]
            qty = min(remaining, lot.qty)
            gross = (exit_price - lot.price) * qty
            fees = fee_per_share * qty
            pnl = gross - fees
            duration = max(0.0, fill["ts"] - lot.ts)
            cost = lot.price * qty
            self._conn.execute(  # type: ignore[union-attr]
                "INSERT INTO trades (market_id, market_label, asset, duration, side, strategy, "
                "qty, entry_price, exit_price, entry_ts, exit_ts, duration_s, gross_pnl, fees, "
                "pnl, return_pct, r_multiple, entry_reason, exit_reason, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fill["market_id"], label, asset, tag_duration, fill["side"], fill["strategy"],
                 qty, lot.price, exit_price, lot.ts, fill["ts"], duration, gross, fees, pnl,
                 (pnl / cost * 100.0) if cost else 0.0,
                 (pnl / cost) if cost else None, None, None, None))
            lot.qty -= qty
            remaining -= qty
            if lot.qty <= 1e-9:
                lots.popleft()
        # A sell with no matching lot (position seeded externally) is recorded as a
        # fill only; there is no cost basis to build a round trip from.


class NullRecorder:
    """No-op recorder so components can be constructed without telemetry."""

    mode = "none"
    strategy_name = ""

    def start(self) -> None: ...
    def stop(self, *, timeout: float = 0.0) -> None: ...
    def register_market_label(self, market_id: str, label: str) -> None: ...
    def register_market_tags(self, market_id: str, asset: str, duration: str) -> None: ...
    def record_order(self, *a: Any, **k: Any) -> None: ...
    def record_fill(self, *a: Any, **k: Any) -> None: ...
    def record_positions(self, *a: Any, **k: Any) -> None: ...
    def record_equity(self, *a: Any, **k: Any) -> None: ...
    def record_heartbeat(self, *a: Any, **k: Any) -> None: ...
    def record_config(self, *a: Any, **k: Any) -> None: ...
    def record_startup(self, *a: Any, **k: Any) -> None: ...
    def record_strategy_state(self, *a: Any, **k: Any) -> None: ...
    def record_event(self, *a: Any, **k: Any) -> None: ...


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return json.dumps({"repr": repr(value)})
