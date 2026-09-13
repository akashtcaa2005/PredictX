"""Offline tests for asset/duration tagging of orders and trades.

Covers: the schema migration that adds asset/duration columns to an
already-existing database (production DBs predate these columns), the
EngineRecorder wiring that stamps them onto order/trade rows, and that the
existing, unmodified group_performance() correctly buckets tagged trades by
asset/duration.
"""
from __future__ import annotations

import time

import pytest

from dashboard.analytics import group_performance
from polybot.orders.models import Action, Order, OrderIntent
from polybot.telemetry.recorder import EngineRecorder
from polybot.telemetry.schema import connect, init_engine_db


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
    deadline = time.time() + timeout
    while not recorder._q.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.6)


def test_migration_adds_columns_to_a_pre_existing_database(tmp_path):
    """Simulate a production DB created before asset/duration existed."""
    db_path = tmp_path / "old.db"
    conn = connect(db_path)
    # The real pre-migration shape: every column orders/trades had before
    # asset/duration were added (see polybot/telemetry/schema.py), so the
    # ENGINE_SCHEMA's CREATE INDEX statements (which reference ts_created etc.)
    # still apply cleanly against it.
    conn.executescript("""
        CREATE TABLE orders (
            client_id TEXT PRIMARY KEY, exchange_order_id TEXT,
            ts_created REAL NOT NULL, ts_updated REAL NOT NULL,
            ts_submitted REAL, ts_terminal REAL,
            market_id TEXT NOT NULL, market_label TEXT,
            side TEXT NOT NULL, token_id TEXT, action TEXT NOT NULL, order_type TEXT,
            price REAL NOT NULL, size REAL NOT NULL,
            filled_size REAL NOT NULL DEFAULT 0, avg_fill_price REAL NOT NULL DEFAULT 0,
            notional REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL, last_error TEXT, exchange_response TEXT,
            execution_ms REAL, simulated INTEGER NOT NULL DEFAULT 0, strategy TEXT
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL, market_label TEXT,
            side TEXT NOT NULL, strategy TEXT, qty REAL NOT NULL,
            entry_price REAL NOT NULL, exit_price REAL NOT NULL,
            entry_ts REAL NOT NULL, exit_ts REAL NOT NULL, duration_s REAL NOT NULL,
            gross_pnl REAL NOT NULL, fees REAL NOT NULL DEFAULT 0, pnl REAL NOT NULL,
            return_pct REAL NOT NULL DEFAULT 0, r_multiple REAL,
            entry_reason TEXT, exit_reason TEXT, confidence REAL
        );
    """)
    conn.commit()
    conn.close()

    # init_engine_db must migrate the existing tables in place, not just skip
    # them because CREATE TABLE IF NOT EXISTS is a no-op.
    migrated = init_engine_db(db_path)
    cols = {row["name"] for row in migrated.execute("PRAGMA table_info(orders)")}
    assert {"asset", "duration"} <= cols
    cols = {row["name"] for row in migrated.execute("PRAGMA table_info(trades)")}
    assert {"asset", "duration"} <= cols
    migrated.close()


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "engine.db"
    init_engine_db(db_path).close()
    # Running it again against an already-migrated DB must not raise
    # "duplicate column name".
    init_engine_db(db_path).close()


def test_register_market_tags_stamps_order_rows(recorder, tmp_path):
    recorder.register_market_tags("M1", "BTC", "5m")
    order = _order()
    recorder.record_order(order)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    row = conn.execute("SELECT asset, duration FROM orders WHERE client_id=?",
                       (order.client_id,)).fetchone()
    assert row["asset"] == "BTC"
    assert row["duration"] == "5m"


def test_untagged_market_leaves_null_columns(recorder, tmp_path):
    order = _order()  # no register_market_tags() call for this market
    recorder.record_order(order)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    row = conn.execute("SELECT asset, duration FROM orders WHERE client_id=?",
                       (order.client_id,)).fetchone()
    assert row["asset"] is None
    assert row["duration"] is None


def test_register_market_tags_stamps_trade_rows(recorder, tmp_path):
    recorder.register_market_tags("M1", "ETH", "15m")
    buy = _order(price=0.40, size=10.0)
    recorder.record_order(buy)
    recorder.record_fill(buy, price=0.40, size=10.0)
    sell = _order(action=Action.SELL, price=0.50, size=10.0)
    recorder.record_order(sell)
    recorder.record_fill(sell, price=0.50, size=10.0, realized_pnl=1.0)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    trade = conn.execute("SELECT asset, duration, pnl FROM trades").fetchone()
    assert trade["asset"] == "ETH"
    assert trade["duration"] == "15m"
    assert trade["pnl"] == pytest.approx(1.0)


def test_group_performance_buckets_by_asset_unchanged(recorder, tmp_path):
    """The existing, unmodified group_performance() must Just Work once trades
    carry an 'asset' field -- no analytics.py changes needed for this feature."""
    for market, asset, entry, exit_, pnl_sign in [
        ("BTC-M", "BTC", 0.40, 0.50, 1), ("BTC-M2", "BTC", 0.40, 0.30, -1),
        ("ETH-M", "ETH", 0.40, 0.60, 1),
    ]:
        recorder.register_market_tags(market, asset, "5m")
        buy = _order(market=market, price=entry, size=10.0)
        recorder.record_order(buy)
        recorder.record_fill(buy, price=entry, size=10.0)
        sell = _order(market=market, action=Action.SELL, price=exit_, size=10.0)
        recorder.record_order(sell)
        recorder.record_fill(sell, price=exit_, size=10.0)
    _drain(recorder)

    conn = connect(tmp_path / "engine.db", read_only=True)
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades").fetchall()]
    by_asset = group_performance(trades, "asset")
    by_name = {g["name"]: g for g in by_asset}
    assert by_name["BTC"]["trades"] == 2
    assert by_name["BTC"]["win_rate"] == 50.0
    assert by_name["ETH"]["trades"] == 1
    assert by_name["ETH"]["win_rate"] == 100.0
