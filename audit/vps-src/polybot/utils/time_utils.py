"""Time helpers — a single source of truth for timestamps across the bot."""
from __future__ import annotations

import time
from datetime import datetime, timezone


def now_s() -> float:
    """Wall-clock seconds since the epoch (float)."""
    return time.time()


def now_ms() -> int:
    """Wall-clock milliseconds since the epoch (int)."""
    return int(time.time() * 1000)


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def iso_to_ts(value: str | None) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds, tolerating trailing ``Z``."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None
