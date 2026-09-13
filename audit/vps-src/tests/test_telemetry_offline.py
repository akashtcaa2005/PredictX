"""Telemetry recorder: durability, round-trip assembly and read-back.

Runs entirely offline against a temporary database.
"""
from __future__ import annotations

import time

import pytest

from polybot.orders.models import Action, Order, OrderIntent, OrderStatus
from polybot.telemetry.recorder import EngineRecorder
from polybot.telemetry.schema import connect


def _order(market="M1", side="up", action=Action.BUY, price=0.40, size=10.0) -> Order:
    return Order(intent=OrderIntent(market_id=market, strategy_side=side, token_id="T-" + side,
                                    action=action, price=price, size=size))


@pytest.fixture()
def recorder(tmp_path):
    rec = EngineRecorder(tmp_path / "engine.db", mode="paper", strategy_name="s1")
    rec.start()
    yield rec
    rec.stop()


def _drain(recorder, timeout=5.0):
    """Wait for the writer thread to flush everything queued."""
    deadline = time.time() + timeout
    while not recorder._q.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.6)  # the writer commits on its idle tick


def test_order_upsert_is_idempotent(recorder, tmp_path):
    order = _order()
    recorder.record_order(order)
    order.status = OrderStatus.FILLED
    order.filled_size = 10.0
    order.touch()
    recorder.record_order(order)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    rows = conn.execute("SELECT client_id, status, filled_size FROM orders").fetchall()
    assert len(rows) == 1, "the same client_id must update in place, not insert twice"
    assert rows[0]["status"] == "filled"
    assert rows[0]["filled_size"] == 10.0


def test_fills_build_round_trip_trades(recorder, tmp_path):
    buy = _order(price=0.40, size=10.0)
    recorder.record_order(buy)
    recorder.record_fill(buy, price=0.40, size=10.0)

    sell = _order(action=Action.SELL, price=0.50, size=10.0)
    recorder.record_order(sell)
    recorder.record_fill(sell, price=0.50, size=10.0, realized_pnl=1.0)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    trades = conn.execute("SELECT * FROM trades").fetchall()
    assert len(trades) == 1
    trade = trades[0]
    assert trade["qty"] == 10.0
    assert trade["entry_price"] == 0.40
    assert trade["exit_price"] == 0.50
    assert trade["pnl"] == pytest.approx(1.0)
    assert trade["return_pct"] == pytest.approx(25.0)


def test_partial_sell_splits_the_lot(recorder, tmp_path):
    buy = _order(price=0.20, size=100.0)
    recorder.record_fill(buy, price=0.20, size=100.0)
    sell = _order(action=Action.SELL, price=0.25, size=40.0)
    recorder.record_fill(sell, price=0.25, size=40.0)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    trades = conn.execute("SELECT qty, pnl FROM trades").fetchall()
    assert len(trades) == 1
    assert trades[0]["qty"] == 40.0
    assert trades[0]["pnl"] == pytest.approx(2.0)  # 40 * (0.25 - 0.20)


def test_fifo_order_is_respected(recorder, tmp_path):
    first = _order(price=0.10, size=10.0)
    recorder.record_fill(first, price=0.10, size=10.0)
    second = _order(price=0.30, size=10.0)
    recorder.record_fill(second, price=0.30, size=10.0)
    sell = _order(action=Action.SELL, price=0.40, size=15.0)
    recorder.record_fill(sell, price=0.40, size=15.0)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    trades = conn.execute("SELECT qty, entry_price, pnl FROM trades ORDER BY id").fetchall()
    assert [t["entry_price"] for t in trades] == [0.10, 0.30], "oldest lot must close first"
    assert [t["qty"] for t in trades] == [10.0, 5.0]
    assert sum(t["pnl"] for t in trades) == pytest.approx(10 * 0.30 + 5 * 0.10)


def test_fees_reduce_trade_pnl(recorder, tmp_path):
    buy = _order(price=0.50, size=10.0)
    recorder.record_fill(buy, price=0.50, size=10.0)
    sell = _order(action=Action.SELL, price=0.60, size=10.0)
    recorder.record_fill(sell, price=0.60, size=10.0, fee=0.25)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    trade = conn.execute("SELECT gross_pnl, fees, pnl FROM trades").fetchone()
    assert trade["gross_pnl"] == pytest.approx(1.0)
    assert trade["fees"] == pytest.approx(0.25)
    assert trade["pnl"] == pytest.approx(0.75)


def test_positions_equity_and_events_round_trip(recorder, tmp_path):
    recorder.record_positions([{
        "market_id": "M1", "side": "up", "market_label": "Up/Down", "token_id": "T-up",
        "qty": 12.0, "avg_price": 0.4, "realized_pnl": 0.0, "mark_price": 0.45,
        "unrealized": 0.6, "ts_updated": time.time(),
    }])
    recorder.record_equity({"balance": 100.0, "cash": 100.0, "positions_value": 5.4,
                            "equity": 105.4, "realized_pnl": 1.0, "unrealized_pnl": 0.6,
                            "exposure": 4.8, "open_orders": 2, "open_positions": 1})
    recorder.record_event("critical", "risk_halt", "daily loss limit reached")
    recorder.record_heartbeat({"mode": "paper", "equity": 105.4})
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    assert conn.execute("SELECT qty FROM positions").fetchone()["qty"] == 12.0
    assert conn.execute("SELECT equity FROM equity").fetchone()["equity"] == 105.4
    assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "risk_halt"
    assert "105.4" in conn.execute(
        "SELECT value FROM engine_state WHERE key='heartbeat'").fetchone()["value"]


def test_lots_survive_a_restart(tmp_path):
    """A restarted engine must not lose the cost basis of open positions."""
    first = EngineRecorder(tmp_path / "engine.db", mode="paper", strategy_name="s1")
    first.start()
    first.record_positions([{
        "market_id": "M1", "side": "up", "market_label": None, "token_id": "T-up",
        "qty": 10.0, "avg_price": 0.30, "realized_pnl": 0.0, "mark_price": 0.30,
        "unrealized": 0.0, "ts_updated": time.time(),
    }])
    _drain(first)
    first.stop()

    second = EngineRecorder(tmp_path / "engine.db", mode="paper", strategy_name="s1")
    second.start()
    sell = _order(action=Action.SELL, price=0.35, size=10.0)
    second.record_fill(sell, price=0.35, size=10.0)
    _drain(second)
    second.stop()

    conn = connect(tmp_path / "engine.db", read_only=True)
    trade = conn.execute("SELECT entry_price, pnl FROM trades").fetchone()
    assert trade is not None, "the restored lot should have produced a round trip"
    assert trade["entry_price"] == pytest.approx(0.30)
    assert trade["pnl"] == pytest.approx(0.5)


def test_recorder_never_raises_on_a_full_queue(tmp_path):
    """Telemetry backpressure must degrade to dropped rows, not an exception."""
    rec = EngineRecorder(tmp_path / "engine.db", mode="paper", max_queue=4)
    # Deliberately not started: nothing drains the queue.
    for _ in range(50):
        rec.record_event("info", "spam", "message")
    assert rec._dropped > 0
