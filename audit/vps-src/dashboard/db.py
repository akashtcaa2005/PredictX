"""Dashboard database: its own schema, plus read-only views onto each engine.

Two distinct concerns live here:

* :class:`DashboardDB` — the dashboard's *own* store (users, sessions, audit
  trail, notifications, trade journal, candles). The dashboard owns and writes it.
* :class:`EngineReader` — a strictly read-only window onto a trading engine's
  telemetry database. It reconnects on demand, so an engine that has not started
  yet (no database file) degrades to "no data" instead of an error.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from polybot.telemetry.schema import connect
from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.db")

DASHBOARD_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'viewer',   -- admin | trader | viewer
    created_ts     REAL NOT NULL,
    last_login_ts  REAL,
    active         INTEGER NOT NULL DEFAULT 1,
    must_change_pw INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    sid          TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf         TEXT NOT NULL,
    created_ts   REAL NOT NULL,
    last_seen_ts REAL NOT NULL,
    expires_ts   REAL NOT NULL,
    ip           TEXT,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    username TEXT,
    ip       TEXT,
    success  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_ts ON login_attempts (ts DESC);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT,
    role    TEXT,
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT,
    ip      TEXT,
    ok      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    severity   TEXT NOT NULL,     -- info | warning | critical
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT,
    mode       TEXT,              -- paper | live | system
    read       INTEGER NOT NULL DEFAULT 0,
    delivered  TEXT               -- comma-separated channels that accepted it
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications (ts DESC);

CREATE TABLE IF NOT EXISTS journal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT NOT NULL,
    trade_id      INTEGER,
    ts            REAL NOT NULL,
    market_id     TEXT,
    market_label  TEXT,
    side          TEXT,
    strategy      TEXT,
    qty           REAL,
    entry_price   REAL,
    exit_price    REAL,
    entry_ts      REAL,
    exit_ts       REAL,
    duration_s    REAL,
    pnl           REAL,
    entry_reason  TEXT,
    exit_reason   TEXT,
    confidence    REAL,
    notes         TEXT,
    rating        INTEGER,
    tags          TEXT,
    entry_shot    TEXT,
    exit_shot     TEXT,
    auto          INTEGER NOT NULL DEFAULT 1,
    UNIQUE (mode, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal (ts DESC);

CREATE TABLE IF NOT EXISTS candles (
    market_key  TEXT NOT NULL,
    interval_s  INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL DEFAULT 0,
    buy_volume  REAL NOT NULL DEFAULT 0,
    sell_volume REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (market_key, interval_s, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles (market_key, ts DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    ts     REAL NOT NULL
);
"""


class DashboardDB:
    """Owns the dashboard's own SQLite store. Writes are serialised by a lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = connect(self.path)
        self._conn.executescript(DASHBOARD_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- generic helpers -------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params)]

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any] | dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def backup_to(self, destination: str | Path) -> Path:
        """Consistent online backup of the dashboard database."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(destination)
            try:
                self._conn.backup(target)
            finally:
                target.close()
        return destination

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    # -- app settings ----------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO app_settings (key, value, ts) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                     (key, value, now_s()))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return row["value"] if row else default

    # -- housekeeping ----------------------------------------------------------
    def prune(self, *, audit_days: int = 90, notification_days: int = 30,
              login_days: int = 14, candle_keep: int = 5_000) -> None:
        """Bound table growth so the database stays small on an 80 GB VPS."""
        cutoff = now_s()
        self.execute("DELETE FROM audit WHERE ts < ?", (cutoff - audit_days * 86400,))
        self.execute("DELETE FROM notifications WHERE ts < ? AND read = 1",
                     (cutoff - notification_days * 86400,))
        self.execute("DELETE FROM login_attempts WHERE ts < ?", (cutoff - login_days * 86400,))
        self.execute(
            "DELETE FROM candles WHERE rowid NOT IN ("
            "  SELECT rowid FROM candles c2 WHERE c2.market_key = candles.market_key "
            "  ORDER BY ts DESC LIMIT ?)", (candle_keep,))
        self.execute("DELETE FROM sessions WHERE expires_ts < ?", (cutoff,))


class EngineReader:
    """Read-only window onto one engine's telemetry database.

    The engine may not have started yet, may be mid-restart, or may have had its
    database removed; every method degrades to empty results rather than raising,
    so the dashboard keeps rendering.
    """

    def __init__(self, mode: str, path: str | Path) -> None:
        self.mode = mode
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._last_attempt = 0.0

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.path.is_file()

    def _connection(self) -> sqlite3.Connection | None:
        with self._lock:
            if self._conn is not None:
                return self._conn
            if not self.path.is_file():
                return None
            # Don't hammer the filesystem when the engine has never run.
            if now_s() - self._last_attempt < 2.0:
                return None
            self._last_attempt = now_s()
            try:
                self._conn = connect(self.path, read_only=True)
            except sqlite3.Error as exc:
                log.debug("engine db unavailable", extra={"mode": self.mode, "error": repr(exc)})
                self._conn = None
            return self._conn

    def _reset(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = self._connection()
        if conn is None:
            return []
        try:
            with self._lock:
                return [dict(row) for row in conn.execute(sql, params)]
        except sqlite3.Error as exc:
            log.debug("engine query failed; will reconnect",
                      extra={"mode": self.mode, "error": repr(exc)})
            self._reset()
            return []

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        value = next(iter(row.values()), default)
        return default if value is None else value

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        self._reset()
