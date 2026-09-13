"""Live market data for the crypto up/down markets the dashboard tracks.

Three jobs:

1. **Discovery** — ask Gamma for active markets and keep the ones that match a
   tracked asset (BTC/ETH/SOL/XRP) and a tracked duration. Duration is inferred
   from the market's real start/end timestamps rather than from its slug, so a
   naming change upstream does not silently empty the dashboard.
2. **Books** — subscribe to the public CLOB ``market`` websocket for every tracked
   outcome token and keep full depth, with a REST snapshot loop as a backstop.
3. **Candles** — sample each market's mid price on a fixed interval into OHLCV
   bars (volume comes from observed trade prints), persist them, and run the
   indicator suite over the series.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from dashboard.config import DashboardSettings
from dashboard.db import DashboardDB
from dashboard.indicators import compute_all
from polybot.api.rest_client import ClientError, RestClient
from polybot.exchange.websocket import ClobWebSocket, market_subscription
from polybot.monitoring.metrics import Metrics
from polybot.utils.retry import RetryPolicy
from polybot.utils.time_utils import iso_to_ts, now_s

log = logging.getLogger("polybot.dashboard.market_feed")

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

_DEPTH_LEVELS = 12


@dataclass
class TrackedMarket:
    """One market the dashboard is watching, with its live state."""

    key: str
    asset: str
    duration: str
    condition_id: str
    question: str
    slug: str
    up_token: str
    down_token: str
    up_outcome: str
    down_outcome: str
    tick_size: float
    min_order_size: float
    neg_risk: bool
    start_ts: float | None
    end_ts: float | None
    volume: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    accepting_orders: bool = True
    closed: bool = False
    discovered_ts: float = field(default_factory=now_s)

    books: dict[str, dict[str, list[tuple[float, float]]]] = field(default_factory=dict)
    book_ts: dict[str, float] = field(default_factory=dict)
    last_trade: dict[str, Any] | None = None
    bucket: dict[str, Any] | None = None
    prev_close: float | None = None

    @property
    def tokens(self) -> tuple[str, str]:
        return (self.up_token, self.down_token)

    def token_side(self, token_id: str) -> str | None:
        if token_id == self.up_token:
            return "up"
        if token_id == self.down_token:
            return "down"
        return None

    def time_remaining(self) -> float | None:
        if self.end_ts is None:
            return None
        return max(0.0, self.end_ts - now_s())

    def quote(self, token_id: str) -> dict[str, Any]:
        """Best bid/ask, mid, spread and depth for one outcome token."""
        book = self.books.get(token_id) or {"bids": [], "asks": []}
        bids, asks = book["bids"], book["asks"]
        bid = bids[0][0] if bids else 0.0
        ask = asks[0][0] if asks else 0.0
        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask or 0.0)
        bid_liq = sum(p * s for p, s in bids)
        ask_liq = sum(p * s for p, s in asks)
        return {
            "token_id": token_id,
            "bid": round(bid, 6),
            "ask": round(ask, 6),
            "mid": round(mid, 6),
            "spread": round(ask - bid, 6) if (bid > 0 and ask > 0) else None,
            "spread_pct": round((ask - bid) / mid * 100.0, 4) if (bid > 0 and ask > 0 and mid) else None,
            "bid_size": round(bids[0][1], 4) if bids else 0.0,
            "ask_size": round(asks[0][1], 4) if asks else 0.0,
            "bid_liquidity": round(bid_liq, 4),
            "ask_liquidity": round(ask_liq, 4),
            "liquidity": round(bid_liq + ask_liq, 4),
            "book_age_s": round(now_s() - self.book_ts.get(token_id, 0.0), 2)
            if self.book_ts.get(token_id) else None,
            "bids": [{"price": round(p, 6), "size": round(s, 4)} for p, s in bids[:_DEPTH_LEVELS]],
            "asks": [{"price": round(p, 6), "size": round(s, 4)} for p, s in asks[:_DEPTH_LEVELS]],
        }

    def mid_price(self) -> float | None:
        """The market's headline price: the mid of the ``up`` outcome."""
        quote = self.quote(self.up_token)
        return quote["mid"] or None

    def status(self) -> str:
        if self.closed:
            return "closed"
        remaining = self.time_remaining()
        if remaining is not None and remaining <= 0:
            return "settling"
        if not self.accepting_orders:
            return "paused"
        if not any(self.books.get(t) for t in self.tokens):
            return "no book"
        return "open"


class MarketFeed:
    """Discovers, subscribes to and summarises the tracked markets."""

    def __init__(self, settings: DashboardSettings, db: DashboardDB) -> None:
        self._s = settings
        self._db = db
        self._metrics = Metrics()
        retry = RetryPolicy(attempts=3, base_delay=0.5, max_delay=5.0)
        self._gamma = RestClient(settings.gamma_host, timeout=settings.http_timeout,
                                 retry_policy=retry, logger=log, metrics=self._metrics,
                                 name="gamma")
        self._clob = RestClient(settings.clob_host, timeout=settings.http_timeout,
                                retry_policy=retry, logger=log, metrics=self._metrics,
                                name="clob_public")
        self._markets: dict[str, TrackedMarket] = {}
        self._by_token: dict[str, TrackedMarket] = {}
        self._ws: ClobWebSocket | None = None
        self._ws_task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

        self.last_discovery_ts = 0.0
        self.last_discovery_error: str | None = None
        self.discovery_count = 0
        self.candle_count = 0

    # ======================================================================
    # Lifecycle
    # ======================================================================
    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.stop()

    async def aclose(self) -> None:
        self.stop()
        if self._ws_task:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
        for client in (self._gamma, self._clob):
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    # ======================================================================
    # Discovery
    # ======================================================================
    async def discovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.discover()
            except Exception as exc:  # noqa: BLE001 - discovery must never die
                self.last_discovery_error = repr(exc)
                log.warning("market discovery failed", extra={"error": repr(exc)})
            await self._sleep(self._s.market_discovery_interval_s)

    async def discover(self) -> int:
        """Refresh the tracked market set from Gamma. Returns the tracked count."""
        rows = await self._fetch_active_markets()
        matched: dict[str, TrackedMarket] = {}
        for row in rows:
            market = self._classify(row)
            if market is None:
                continue
            existing = self._markets.get(market.key)
            if existing is not None:
                # Preserve live book/candle state across rediscovery.
                market.books = existing.books
                market.book_ts = existing.book_ts
                market.bucket = existing.bucket
                market.prev_close = existing.prev_close
                market.last_trade = existing.last_trade
                market.discovered_ts = existing.discovered_ts
            matched[market.key] = market

        # Prefer markets that resolve soonest — those are the tradeable ones.
        ordered = sorted(matched.values(),
                         key=lambda m: (m.end_ts if m.end_ts is not None else float("inf")))
        keep = ordered[: self._s.max_markets_tracked]

        async with self._lock:
            previous_tokens = set(self._by_token)
            self._markets = {m.key: m for m in keep}
            self._by_token = {}
            for market in keep:
                for token in market.tokens:
                    if token:
                        self._by_token[token] = market
            changed = set(self._by_token) != previous_tokens

        self.last_discovery_ts = now_s()
        self.discovery_count += 1
        self.last_discovery_error = None
        if changed:
            log.info("tracked markets changed", extra={"count": len(keep),
                     "assets": sorted({m.asset for m in keep})})
            await self._resubscribe()
            await self._snapshot_books()
        return len(keep)

    #: Gamma caps ``limit`` at 100 regardless of what is requested, so the page
    #: size must match or paging silently stops after the first batch.
    _PAGE = 100
    _MAX_PAGES = 10

    async def _fetch_active_markets(self) -> list[dict[str, Any]]:
        """Pull active, unclosed markets from Gamma.

        Ordered by ``startDate`` *descending* — most recently listed first. This
        matters: the timed up/down markets are re-listed continuously, and
        ordering by ``endDate`` surfaces a long tail of already-settled windows
        instead (measured: 55 live matches by endDate order vs 217 by startDate
        order over the same 800 rows).

        Gamma refuses offsets beyond a server-side limit (HTTP 422, "use
        /markets/keyset for deeper pagination"). That is a stop condition, not an
        error — we already hold the newest pages, which is where the live windows
        are.
        """
        collected: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self._MAX_PAGES):
            try:
                batch = await self._gamma.get("/markets", params={
                    "active": "true", "closed": "false", "archived": "false",
                    "limit": self._PAGE, "offset": offset,
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
            if len(batch) < self._PAGE:
                break
            offset += len(batch)
        return collected

    def _classify(self, row: dict[str, Any]) -> TrackedMarket | None:
        """Turn a Gamma market row into a TrackedMarket, or ``None`` if unwanted."""
        slug = str(row.get("slug") or "")
        question = str(row.get("question") or row.get("title") or "")
        haystack = f"{slug} {question}".lower()

        asset = _match_asset(haystack, self._s.market_assets)
        if asset is None:
            return None

        # The slug is authoritative for this market family:
        #   eth-updown-5m-1766161800  ->  5-minute window starting at that unix ts.
        # Gamma's startDate/endDate describe when the market was *listed* and when
        # it must be *resolved* — for a 5m market those span ~24h and say nothing
        # about the tradeable window, so they must not drive duration detection.
        window = _parse_window(slug)
        if window is not None:
            duration, start_ts, end_ts = window
            if duration not in {d.lower() for d in self._s.market_durations}:
                return None
        else:
            start_ts = iso_to_ts(row.get("startDate") or row.get("start_date_iso")
                                 or row.get("gameStartTime"))
            end_ts = iso_to_ts(row.get("endDate") or row.get("end_date_iso"))
            duration = _match_duration(start_ts, end_ts, haystack, self._s.market_durations)
            if duration is None:
                return None

        # Skip windows that have already closed; Gamma keeps them 'active' until
        # they are resolved, and a settled market is not tradeable.
        if end_ts is not None and end_ts <= now_s():
            return None

        tokens = _json_list(row.get("clobTokenIds"))
        outcomes = _json_list(row.get("outcomes")) or ["Up", "Down"]
        if len(tokens) < 2:
            return None

        condition_id = str(row.get("conditionId") or row.get("condition_id") or "")
        return TrackedMarket(
            key=f"{asset}-{duration}-{condition_id[:12] or slug[:24]}",
            asset=asset,
            duration=duration,
            condition_id=condition_id,
            question=question,
            slug=slug,
            up_token=str(tokens[0]),
            down_token=str(tokens[1]),
            up_outcome=str(outcomes[0]),
            down_outcome=str(outcomes[1] if len(outcomes) > 1 else "Down"),
            tick_size=_f(row.get("orderPriceMinTickSize") or row.get("minimum_tick_size"), 0.01),
            min_order_size=_f(row.get("orderMinSize") or row.get("minimum_order_size"), 5.0),
            neg_risk=bool(row.get("negRisk") or row.get("neg_risk")),
            start_ts=start_ts,
            end_ts=end_ts,
            volume=_f(row.get("volumeNum") or row.get("volume"), 0.0),
            volume_24h=_f(row.get("volume24hr") or row.get("volume24hrClob"), 0.0),
            liquidity=_f(row.get("liquidityNum") or row.get("liquidity"), 0.0),
            accepting_orders=bool(row.get("acceptingOrders", True)),
            closed=bool(row.get("closed")),
        )

    # ======================================================================
    # Books
    # ======================================================================
    async def _resubscribe(self) -> None:
        """Rebuild the market websocket for the current token set."""
        tokens = sorted(self._by_token)
        if self._ws is not None:
            self._ws.stop()
        if self._ws_task is not None:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
            self._ws_task = None
        if not tokens:
            self._ws = None
            return
        self._ws = ClobWebSocket(
            self._s.clob_ws_url, channel="market",
            subscribe_message=market_subscription(tokens),
            on_message=self._on_market_event, logger=log, metrics=self._metrics,
            ping_interval=10.0, stale_timeout=60.0, max_backoff=30.0)
        self._ws_task = asyncio.create_task(self._ws.run(), name="dashboard-market-ws")

    async def _on_market_event(self, event: dict[str, Any]) -> None:
        token = str(event.get("asset_id") or event.get("assetId") or "")
        market = self._by_token.get(token)
        if market is None:
            return
        event_type = str(event.get("event_type") or event.get("type") or "").lower()

        if event_type in ("book", "") and (event.get("bids") is not None or event.get("asks") is not None):
            market.books[token] = {
                "bids": sorted(_levels(event.get("bids")), key=lambda lvl: -lvl[0]),
                "asks": sorted(_levels(event.get("asks")), key=lambda lvl: lvl[0]),
            }
            market.book_ts[token] = now_s()
        elif event_type == "price_change":
            self._apply_price_change(market, token, event)
        elif event_type in ("last_trade_price", "trade"):
            price = _f(event.get("price"), 0.0)
            size = _f(event.get("size"), 0.0)
            side = str(event.get("side") or "").upper()
            market.last_trade = {"price": price, "size": size, "side": side, "ts": now_s(),
                                 "token_id": token}
            self._record_trade_volume(market, token, price, size, side)

    def _apply_price_change(self, market: TrackedMarket, token: str, event: Any) -> None:
        """Apply an incremental level update to the cached book."""
        book = market.books.setdefault(token, {"bids": [], "asks": []})
        changes = event.get("changes") or event.get("price_changes") or []
        if isinstance(changes, dict):
            changes = [changes]
        for change in changes:
            try:
                price = float(change.get("price"))
                size = float(change.get("size"))
            except (TypeError, ValueError, AttributeError):
                continue
            side = str(change.get("side") or "").upper()
            key = "bids" if side in ("BUY", "BID") else "asks"
            levels = [lvl for lvl in book[key] if abs(lvl[0] - price) > 1e-9]
            if size > 0:
                levels.append((price, size))
            book[key] = sorted(levels, key=lambda lvl: -lvl[0] if key == "bids" else lvl[0])
        market.book_ts[token] = now_s()

    async def book_loop(self) -> None:
        """REST snapshot backstop; also covers the gap before the socket connects."""
        while not self._stop.is_set():
            try:
                await self._snapshot_books(only_stale=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("book snapshot failed", extra={"error": repr(exc)})
            await self._sleep(max(2.0, self._s.market_book_interval_s * 5))

    async def _snapshot_books(self, *, only_stale: bool = False) -> None:
        """Fetch full books over REST for tokens the socket has not covered."""
        async with self._lock:
            markets = list(self._markets.values())
        stale_cutoff = now_s() - 15.0
        tokens = [
            (market, token)
            for market in markets for token in market.tokens
            if token and (not only_stale or market.book_ts.get(token, 0.0) < stale_cutoff)
        ]
        for market, token in tokens[:40]:  # bound the burst so a restart is cheap
            try:
                book = await self._clob.get("/book", params={"token_id": token})
            except Exception as exc:  # noqa: BLE001
                log.debug("rest book failed", extra={"token": token[:12], "error": repr(exc)})
                continue
            if not isinstance(book, dict):
                continue
            market.books[token] = {
                "bids": sorted(_levels(book.get("bids")), key=lambda lvl: -lvl[0]),
                "asks": sorted(_levels(book.get("asks")), key=lambda lvl: lvl[0]),
            }
            market.book_ts[token] = now_s()

    # ======================================================================
    # Candles
    # ======================================================================
    def _record_trade_volume(self, market: TrackedMarket, token: str, price: float,
                             size: float, side: str) -> None:
        if market.bucket is None or token != market.up_token:
            return
        market.bucket["volume"] += size
        if side == "BUY":
            market.bucket["buy_volume"] += size
        elif side == "SELL":
            market.bucket["sell_volume"] += size

    async def candle_loop(self) -> None:
        """Roll one OHLCV bar per market per ``candle_interval_s``."""
        interval = max(5, self._s.candle_interval_s)
        while not self._stop.is_set():
            try:
                self._sample_candles(interval)
            except Exception as exc:  # noqa: BLE001
                log.warning("candle sampling failed", extra={"error": repr(exc)})
            await self._sleep(1.0)

    def _sample_candles(self, interval: int) -> None:
        now = now_s()
        bucket_ts = int(now // interval * interval)
        rows: list[tuple[Any, ...]] = []
        for market in list(self._markets.values()):
            price = market.mid_price()
            if not price:
                continue
            bucket = market.bucket
            if bucket is None or bucket["ts"] != bucket_ts:
                if bucket is not None:
                    rows.append((market.key, interval, bucket["ts"], bucket["open"],
                                 bucket["high"], bucket["low"], bucket["close"],
                                 bucket["volume"], bucket["buy_volume"], bucket["sell_volume"]))
                    market.prev_close = bucket["close"]
                market.bucket = {"ts": bucket_ts, "open": price, "high": price, "low": price,
                                 "close": price, "volume": 0.0, "buy_volume": 0.0,
                                 "sell_volume": 0.0}
            else:
                bucket["high"] = max(bucket["high"], price)
                bucket["low"] = min(bucket["low"], price)
                bucket["close"] = price
        if rows:
            self._db.executemany(
                "INSERT OR REPLACE INTO candles (market_key, interval_s, ts, open, high, low, "
                "close, volume, buy_volume, sell_volume) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self.candle_count += len(rows)

    def candles(self, market_key: str, limit: int = 300) -> list[dict[str, Any]]:
        """Stored candles for a market, oldest first, with the in-progress bar appended."""
        rows = self._db.query(
            "SELECT ts, open, high, low, close, volume, buy_volume, sell_volume FROM candles "
            "WHERE market_key = ? AND interval_s = ? ORDER BY ts DESC LIMIT ?",
            (market_key, max(5, self._s.candle_interval_s), limit))
        rows.reverse()
        market = self._markets.get(market_key)
        if market and market.bucket:
            rows.append(dict(market.bucket))
        return rows

    # ======================================================================
    # Snapshot
    # ======================================================================
    def snapshot(self, *, with_indicators: bool = True, with_depth: bool = True) -> dict[str, Any]:
        """Everything the Markets tab renders."""
        markets = []
        for market in sorted(self._markets.values(),
                             key=lambda m: (m.asset, m.duration,
                                            m.end_ts if m.end_ts is not None else 0)):
            up = market.quote(market.up_token)
            down = market.quote(market.down_token)
            if not with_depth:
                up.pop("bids", None), up.pop("asks", None)
                down.pop("bids", None), down.pop("asks", None)
            mid = market.mid_price() or 0.0
            change = None
            if market.prev_close:
                change = round((mid - market.prev_close) / market.prev_close * 100.0, 4)
            row: dict[str, Any] = {
                "key": market.key,
                "asset": market.asset,
                "duration": market.duration,
                "condition_id": market.condition_id,
                "question": market.question,
                "slug": market.slug,
                "price": round(mid, 6),
                "price_change_pct": change,
                "up": up,
                "down": down,
                "up_outcome": market.up_outcome,
                "down_outcome": market.down_outcome,
                "tick_size": market.tick_size,
                "min_order_size": market.min_order_size,
                "neg_risk": market.neg_risk,
                "volume": market.volume,
                "volume_24h": market.volume_24h,
                "liquidity": round(up["liquidity"] + down["liquidity"], 4) or market.liquidity,
                "gamma_liquidity": market.liquidity,
                "time_remaining_s": market.time_remaining(),
                "settlement_ts": market.end_ts,
                "start_ts": market.start_ts,
                "status": market.status(),
                "last_trade": market.last_trade,
            }
            if with_indicators:
                row["indicators"] = compute_all(self.candles(market.key))
            markets.append(row)

        return {
            "markets": markets,
            "count": len(markets),
            "assets": list(self._s.market_assets),
            "durations": list(self._s.market_durations),
            "websocket": self._ws.status if self._ws else {"connected": False,
                                                           "channel": "market"},
            "discovery": {
                "last_ts": self.last_discovery_ts,
                "count": self.discovery_count,
                "error": self.last_discovery_error,
                "tracked": len(self._markets),
                "limit": self._s.max_markets_tracked,
            },
            "candles_written": self.candle_count,
        }

    def market_keys(self) -> list[str]:
        return list(self._markets)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


# ======================================================================
# Helpers
# ======================================================================
def _match_asset(haystack: str, assets: Iterable[str]) -> str | None:
    """Identify the asset a market refers to, using word-boundary matching."""
    for asset in assets:
        for keyword in ASSET_KEYWORDS.get(asset.upper(), (asset.lower(),)):
            if re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", haystack):
                return asset.upper()
    return None


#: ``btc-updown-5m-1766161800`` / ``eth-up-or-down-15m-1766161800``
_WINDOW_SLUG = re.compile(r"-(?:updown|up-or-down)-(\d+)(m|h)-(\d{9,12})\b")


def _parse_window(slug: str) -> tuple[str, float, float] | None:
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


def _match_duration(start_ts: float | None, end_ts: float | None, haystack: str,
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


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for level in raw or ():
        try:
            if isinstance(level, dict):
                price, size = float(level.get("price", 0)), float(level.get("size", 0))
            elif isinstance(level, (list, tuple)):
                price, size = float(level[0]), float(level[1])
            else:
                price, size = float(level.price), float(level.size)
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if price > 0 and size > 0:
            out.append((price, size))
    return out


def _f(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # reject NaN
