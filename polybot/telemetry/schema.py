"""SQLite schema + connection helpers for the per-engine telemetry database.

One database per trading mode (``data/paper/engine.db`` / ``data/live/engine.db``).
The engine process is the sole writer; readers (the dashboard) open the same file
with ``mode=ro`` and rely on WAL for lock-free concurrent reads.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

#: DDL executed on every engine start (idempotent).
ENGINE_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- Every order the engine has ever created, live or simulated. -----------------
CREATE TABLE IF NOT EXISTS orders (
    client_id          TEXT PRIMARY KEY,
    exchange_order_id  TEXT,
    ts_created         REAL NOT NULL,
    ts_updated         REAL NOT NULL,
    ts_submitted       REAL,
    ts_terminal        REAL,
    market_id          TEXT NOT NULL,
    market_label       TEXT,
    asset              TEXT,                   -- 'BTC' | 'ETH' | 'SOL' | 'XRP' | NULL
    duration           TEXT,                   -- '5m' | '15m' | ... | NULL
    side               TEXT NOT NULL,          -- 'up' | 'down' (outcome token)
    token_id           TEXT,
    action             TEXT NOT NULL,          -- 'BUY' | 'SELL'
    order_type         TEXT,
    price              REAL NOT NULL,
    size               REAL NOT NULL,
    filled_size        REAL NOT NULL DEFAULT 0,
    avg_fill_price     REAL NOT NULL DEFAULT 0,
    notional           REAL NOT NULL DEFAULT 0,
    fees               REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL,
    last_error         TEXT,
    exchange_response  TEXT,
    execution_ms       REAL,
    simulated          INTEGER NOT NULL DEFAULT 0,
    strategy           TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (ts_created DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_market  ON orders (market_id);

-- Individual fills (partial or complete). -------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 REAL NOT NULL,
    client_id          TEXT,
    exchange_order_id  TEXT,
    market_id          TEXT NOT NULL,
    side               TEXT NOT NULL,
    token_id           TEXT,
    action             TEXT NOT NULL,
    price              REAL NOT NULL,
    size               REAL NOT NULL,
    fee                REAL NOT NULL DEFAULT 0,
    realized_pnl       REAL NOT NULL DEFAULT 0,
    strategy           TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills (ts DESC);

-- Completed round trips, assembled from the fill stream (FIFO lots). ----------
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     TEXT NOT NULL,
    market_label  TEXT,
    asset         TEXT,                        -- 'BTC' | 'ETH' | 'SOL' | 'XRP' | NULL
    duration      TEXT,                        -- '5m' | '15m' | ... | NULL
    side          TEXT NOT NULL,
    strategy      TEXT,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    entry_ts      REAL NOT NULL,
    exit_ts       REAL NOT NULL,
    duration_s    REAL NOT NULL,
    gross_pnl     REAL NOT NULL,
    fees          REAL NOT NULL DEFAULT 0,
    pnl           REAL NOT NULL,
    return_pct    REAL NOT NULL DEFAULT 0,
    r_multiple    REAL,
    entry_reason  TEXT,
    exit_reason   TEXT,
    confidence    REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades (exit_ts DESC);

-- Current positions, one row per (market, outcome side). ----------------------
CREATE TABLE IF NOT EXISTS positions (
    market_id     TEXT NOT NULL,
    side          TEXT NOT NULL,
    market_label  TEXT,
    token_id      TEXT,
    qty           REAL NOT NULL DEFAULT 0,
    avg_price     REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    mark_price    REAL NOT NULL DEFAULT 0,
    unrealized    REAL NOT NULL DEFAULT 0,
    ts_updated    REAL NOT NULL,
    PRIMARY KEY (market_id, side)
);

-- Point-in-time portfolio samples; the source of every equity/PnL chart. ------
CREATE TABLE IF NOT EXISTS equity (
    ts               REAL PRIMARY KEY,
    balance          REAL NOT NULL DEFAULT 0,
    cash             REAL NOT NULL DEFAULT 0,
    positions_value  REAL NOT NULL DEFAULT 0,
    equity           REAL NOT NULL DEFAULT 0,
    realized_pnl     REAL NOT NULL DEFAULT 0,
    unrealized_pnl   REAL NOT NULL DEFAULT 0,
    exposure         REAL NOT NULL DEFAULT 0,
    open_orders      INTEGER NOT NULL DEFAULT 0,
    open_positions   INTEGER NOT NULL DEFAULT 0
);

-- Latest strategy view per (strategy, market). --------------------------------
CREATE TABLE IF NOT EXISTS strategy_state (
    strategy        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    market_label    TEXT,
    ts              REAL NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    running         INTEGER NOT NULL DEFAULT 0,
    last_signal     TEXT,
    last_signal_ts  REAL,
    current_signal  TEXT,
    confidence      REAL,
    position_qty    REAL NOT NULL DEFAULT 0,
    entry_price     REAL,
    exit_price      REAL,
    stop_loss       REAL,
    take_profit     REAL,
    unrealized      REAL NOT NULL DEFAULT 0,
    ticks           INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, market_id)
);

-- Notable engine events; the dashboard turns these into alerts. ---------------
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL,       -- info | warning | critical
    kind     TEXT NOT NULL,       -- machine-readable event key
    message  TEXT NOT NULL,
    data     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);

-- Single-row key/value store: 'heartbeat', 'config', 'startup'. ---------------
CREATE TABLE IF NOT EXISTS engine_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    ts     REAL NOT NULL
);
"""


def connect(path: str | Path, *, read_only: bool = False, timeout: float = 10.0) -> sqlite3.Connection:
    """Open ``path`` with row access by name and WAL-friendly pragmas.

    ``read_only`` opens the file via a ``file:...?mode=ro`` URI so a reader can
    never mutate an engine database, and never blocks the writing engine.
    """
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout,
                               check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


#: Columns added after a table's original CREATE TABLE shipped. `CREATE TABLE IF
#: NOT EXISTS` is a no-op against an already-existing table, so a plain schema
#: edit alone never reaches a production database -- these must be migrated in
#: explicitly, once, via ALTER TABLE.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "orders": (("asset", "TEXT"), ("duration", "TEXT")),
    "trades": (("asset", "TEXT"), ("duration", "TEXT")),
}


def _migrate_added_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def init_engine_db(path: str | Path) -> sqlite3.Connection:
    """Create/upgrade an engine database and return an open writable connection."""
    conn = connect(path)
    conn.executescript(ENGINE_SCHEMA)
    _migrate_added_columns(conn)
    conn.commit()
    return conn
