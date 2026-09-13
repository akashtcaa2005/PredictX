"""Structured, multi-file logging.

Each concern writes to its own file under ``LOG_DIR`` with timestamps on every
record, plus a console mirror and a consolidated ``errors.log`` capturing
WARNING+ from everywhere:

    trade.log       filled/closed trades and PnL
    orders.log      order placement / cancel / replace / status transitions
    strategy.log    strategy ticks and signals
    execution.log   engine lifecycle, startup/shutdown, health
    websocket.log   websocket connects / messages / reconnects
    wallet.log      wallet balance / allowance / maker / approval
    api.log         REST API calls (Gamma / data-API / Camber)
    errors.log      all WARNING+ records (aggregated)

Use :func:`setup_logging` once at startup, then :func:`get_logger` (or the
convenience accessors) anywhere.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Per-file rotation so no single domain log can grow unbounded and fill the disk.
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_LOG_BACKUPS = 5                   # keep 5 rotations (~60 MB per domain worst case)

# Domain logger names -> log file names.
_DOMAINS = {
    "trade": "trade.log",
    "orders": "orders.log",
    "strategy": "strategy.log",
    "execution": "execution.log",
    "websocket": "websocket.log",
    "wallet": "wallet.log",
    "api": "api.log",
}
_ROOT = "polybot"
_configured = False


class _KeyValueFormatter(logging.Formatter):
    """`timestamp level logger | message | k=v k=v` with ISO-8601 UTC time."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds")
        base = f"{ts} {record.levelname:<7} {record.name} | {record.getMessage()}"
        extra = _extra_fields(record)
        if extra:
            kv = " ".join(f"{k}={_fmt(v)}" for k, v in extra.items())
            base = f"{base} | {kv}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


class _JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_extra_fields(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}


def _fmt(value: object) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


def setup_logging(log_dir: str | Path, level: str = "INFO", json_format: bool = False) -> None:
    """Configure the ``polybot`` logger tree. Idempotent."""
    global _configured
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter: logging.Formatter = _JsonFormatter() if json_format else _KeyValueFormatter()
    level_num = getattr(logging, str(level).upper(), logging.INFO)

    root = logging.getLogger(_ROOT)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(level_num)
    console.setFormatter(formatter)
    root.addHandler(console)

    errors = RotatingFileHandler(log_dir / "errors.log", maxBytes=_LOG_MAX_BYTES,
                                 backupCount=_LOG_BACKUPS, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    errors.setFormatter(formatter)
    root.addHandler(errors)

    # Per-domain child loggers each get a dedicated file; they still propagate to
    # the root's console + errors handlers.
    for domain, filename in _DOMAINS.items():
        child = logging.getLogger(f"{_ROOT}.{domain}")
        child.setLevel(logging.DEBUG)
        child.propagate = True
        for handler in list(child.handlers):
            child.removeHandler(handler)
        file_handler = RotatingFileHandler(log_dir / filename, maxBytes=_LOG_MAX_BYTES,
                                           backupCount=_LOG_BACKUPS, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        child.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``polybot`` tree (e.g. ``get_logger("orders")``)."""
    if name == _ROOT or name.startswith(f"{_ROOT}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT}.{name}")


# Convenience accessors for the domain loggers.
def trade_logger() -> logging.Logger: return get_logger("trade")
def orders_logger() -> logging.Logger: return get_logger("orders")
def strategy_logger() -> logging.Logger: return get_logger("strategy")
def execution_logger() -> logging.Logger: return get_logger("execution")
def websocket_logger() -> logging.Logger: return get_logger("websocket")
