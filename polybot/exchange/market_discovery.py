"""Discover currently-tradeable crypto 5m/15m markets for the trading engine.

Same Gamma-polling approach dashboard/market_feed.py uses for its read-only
display tracking, reused here to decide which markets the engine itself
should register and trade. This module only ever reads from Gamma -- it
never touches order flow, risk, or the strategy.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from polybot.api.rest_client import ClientError, RestClient
from polybot.exchange.market_classify import ClassifiedMarket, classify

log = logging.getLogger("polybot.exchange.market_discovery")

_PAGE = 100
_MAX_PAGES = 10


async def fetch_active_rows(gamma: RestClient) -> list[dict[str, Any]]:
    """Pull active, unclosed markets from Gamma (paginated, newest-listed first).

    Mirrors dashboard/market_feed.py's _fetch_active_markets(): ordering by
    startDate descending surfaces the live windows; endDate order surfaces a
    long tail of already-settled ones instead. A "offset too large" ClientError
    (Gamma's paging ceiling) is a normal stop condition, not an error.
    """
    collected: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        try:
            batch = await gamma.get("/markets", params={
                "active": "true", "closed": "false", "archived": "false",
                "limit": _PAGE, "offset": offset,
                "order": "startDate", "ascending": "false",
            })
        except ClientError as exc:
            if "offset too large" in str(exc).lower():
                log.debug("gamma paging limit reached", extra={"offset": offset})
                break
            raise
        if not isinstance(batch, list) or not batch:
            break
        collected.extend(batch)
        if len(batch) < _PAGE:
            break
        offset += len(batch)
    return collected


async def discover_eligible_markets(gamma: RestClient, *, assets: Iterable[str],
                                    durations: Iterable[str]) -> list[ClassifiedMarket]:
    """Return currently-open markets matching the configured assets/durations.

    Ranked soonest-to-resolve first (the actually-tradeable ones), deduplicated
    by condition_id. Empty ``assets``/``durations`` yields an empty result --
    callers should treat that as "discovery disabled", not "nothing found".
    """
    assets = tuple(assets)
    durations = tuple(durations)
    if not assets or not durations:
        return []

    rows = await fetch_active_rows(gamma)
    by_condition: dict[str, ClassifiedMarket] = {}
    for row in rows:
        market = classify(row, assets, durations)
        if market is None:
            continue
        by_condition[market.condition_id] = market

    return sorted(by_condition.values(),
                  key=lambda m: (m.end_ts if m.end_ts is not None else float("inf")))
