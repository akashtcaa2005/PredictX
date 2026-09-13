"""Classify a raw Gamma market row into a tracked crypto asset + duration.

Ported from dashboard/market_feed.py's discovery/classification logic (which
solves this same problem for the dashboard's read-only display). This module
is the pure-function core shared by the engine's market-discovery loop; it has
no I/O and no dependency on dashboard code.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from polybot.utils.time_utils import iso_to_ts, now_s

#: Keywords that identify each tracked asset in a market slug or question.
ASSET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BTC": ("btc", "bitcoin"),
    "ETH": ("eth", "ethereum", "ether"),
    "SOL": ("sol", "solana"),
    "XRP": ("xrp", "ripple"),
}

#: Duration label -> (nominal seconds, tolerance seconds).
DURATIONS: dict[str, tuple[float, float]] = {
    "1m": (60, 20),
    "5m": (300, 90),
    "15m": (900, 180),
    "30m": (1800, 300),
    "1h": (3600, 600),
}

#: ``btc-updown-5m-1766161800`` / ``eth-up-or-down-15m-1766161800``
_WINDOW_SLUG = re.compile(r"-(?:updown|up-or-down)-(\d+)(m|h)-(\d{9,12})\b")


@dataclass(frozen=True)
class ClassifiedMarket:
    """A Gamma market row identified as a tradeable, currently-open crypto window."""

    condition_id: str
    asset: str
    duration: str
    question: str
    slug: str
    up_token: str
    down_token: str
    tick_size: float
    min_order_size: float
    neg_risk: bool
    end_ts: float | None
    accepting_orders: bool


def match_asset(haystack: str, assets: Iterable[str]) -> str | None:
    """Identify the asset a market refers to, using word-boundary matching."""
    for asset in assets:
        for keyword in ASSET_KEYWORDS.get(asset.upper(), (asset.lower(),)):
            if re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", haystack):
                return asset.upper()
    return None


def parse_window(slug: str) -> tuple[str, float, float] | None:
    """Extract ``(duration_label, window_start_ts, window_end_ts)`` from a slug.

    Returns ``None`` when the slug is not one of the timed up/down markets.
    """
    match = _WINDOW_SLUG.search(slug.lower())
    if match is None:
        return None
    count, unit, start_raw = int(match.group(1)), match.group(2), int(match.group(3))
    seconds = count * (60 if unit == "m" else 3600)
    if seconds <= 0:
        return None
    return f"{count}{unit}", float(start_raw), float(start_raw + seconds)


def match_duration(start_ts: float | None, end_ts: float | None, haystack: str,
                   wanted: Iterable[str]) -> str | None:
    """Infer a market's duration from its timestamps, falling back to its text."""
    wanted_set = {d.lower() for d in wanted}
    if start_ts and end_ts and end_ts > start_ts:
        span = end_ts - start_ts
        for label, (nominal, tolerance) in DURATIONS.items():
            if label in wanted_set and abs(span - nominal) <= tolerance:
                return label
        return None
    # No usable timestamps: fall back to an explicit duration in the slug/question.
    for label in wanted_set:
        if re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", haystack):
            return label
    for label, phrase in (("5m", "5 minute"), ("15m", "15 minute"), ("1h", "hourly")):
        if label in wanted_set and phrase in haystack:
            return label
    return None


def _json_list(value: Any) -> list[Any]:
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _f(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # reject NaN


def classify(row: dict[str, Any], assets: Iterable[str], durations: Iterable[str]
            ) -> ClassifiedMarket | None:
    """Turn a Gamma market row into a ClassifiedMarket, or ``None`` if unwanted.

    Same rules dashboard/market_feed.py's ``_classify`` uses: asset+duration
    must both be in the wanted sets, the slug's own encoded window is
    authoritative for duration when present (Gamma's startDate/endDate span
    the listing/resolution deadline, not the actual tradeable window), and an
    already-closed window (``end_ts <= now``) is rejected even if Gamma still
    reports it active.
    """
    slug = str(row.get("slug") or "")
    question = str(row.get("question") or row.get("title") or "")
    haystack = f"{slug} {question}".lower()

    asset = match_asset(haystack, assets)
    if asset is None:
        return None

    window = parse_window(slug)
    if window is not None:
        duration, _start_ts, end_ts = window
        if duration not in {d.lower() for d in durations}:
            return None
    else:
        start_ts = iso_to_ts(row.get("startDate") or row.get("start_date_iso")
                             or row.get("gameStartTime"))
        end_ts = iso_to_ts(row.get("endDate") or row.get("end_date_iso"))
        duration = match_duration(start_ts, end_ts, haystack, durations)
        if duration is None:
            return None

    if end_ts is not None and end_ts <= now_s():
        return None

    tokens = _json_list(row.get("clobTokenIds"))
    if len(tokens) < 2:
        return None

    condition_id = str(row.get("conditionId") or row.get("condition_id") or "")
    if not condition_id:
        return None

    return ClassifiedMarket(
        condition_id=condition_id,
        asset=asset,
        duration=duration,
        question=question,
        slug=slug,
        up_token=str(tokens[0]),
        down_token=str(tokens[1]),
        tick_size=_f(row.get("orderPriceMinTickSize") or row.get("minimum_tick_size"), 0.01),
        min_order_size=_f(row.get("orderMinSize") or row.get("minimum_order_size"), 5.0),
        neg_risk=bool(row.get("negRisk") or row.get("neg_risk")),
        end_ts=end_ts,
        accepting_orders=bool(row.get("acceptingOrders", True)),
    )
