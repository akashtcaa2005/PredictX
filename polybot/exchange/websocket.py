"""Resilient CLOB websocket client.

One :class:`ClobWebSocket` drives a single channel (``market`` or ``user``). It:

* connects and (re)sends the subscription payload on every (re)connect
  (subscription *resume*),
* auto-reconnects with exponential backoff on any drop,
* uses protocol pings as a heartbeat and treats a silent socket (no message
  within ``stale_timeout``) as dead, forcing a reconnect,
* dispatches every decoded message to an async callback.

The engine runs two of these: a public ``market`` feed (order books) and an
authenticated ``user`` feed (order/trade updates).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

from polybot.monitoring.metrics import Metrics
from polybot.utils.time_utils import now_s

MessageHandler = Callable[[Any], Awaitable[None]]


class ClobWebSocket:
    """A single auto-reconnecting websocket channel."""

    def __init__(
        self,
        url: str,
        *,
        channel: str,
        subscribe_message: dict[str, Any],
        on_message: MessageHandler,
        logger: logging.Logger | None = None,
        metrics: Metrics | None = None,
        ping_interval: float = 10.0,
        stale_timeout: float = 45.0,
        max_backoff: float = 30.0,
    ) -> None:
        self._url = url.rstrip("/") + f"/{channel}"
        self._channel = channel
        self._subscribe_message = subscribe_message
        self._on_message = on_message
        self._log = logger or logging.getLogger("polybot.websocket")
        self._metrics = metrics
        self._ping_interval = ping_interval
        self._stale_timeout = stale_timeout
        self._max_backoff = max_backoff

        self._stop = asyncio.Event()
        self.connected = False
        self.last_message_ts = 0.0
        self.reconnects = 0

    @property
    def status(self) -> dict[str, Any]:
        return {
            "channel": self._channel,
            "connected": self.connected,
            "last_message_ts": self.last_message_ts,
            "age_s": round(now_s() - self.last_message_ts, 1) if self.last_message_ts else None,
            "reconnects": self.reconnects,
        }

    async def run(self) -> None:
        """Connect/serve/reconnect until :meth:`stop` is called."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._serve()
                backoff = 1.0  # clean exit (stop requested)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.connected = False
                self.reconnects += 1
                if self._metrics:
                    self._metrics.incr(f"ws.{self._channel}.reconnect")
                self._log.warning("ws disconnected; reconnecting",
                                  extra={"channel": self._channel, "error": repr(exc),
                                         "backoff_s": round(backoff, 1)})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(self._max_backoff, backoff * 2)

    async def _serve(self) -> None:
        async with websockets.connect(
            self._url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_interval * 2,
            close_timeout=5,
            max_size=None,
        ) as ws:
            self.connected = True
            self.last_message_ts = now_s()
            await ws.send(json.dumps(self._subscribe_message))
            self._log.info("ws connected + subscribed",
                           extra={"channel": self._channel, "url": self._url})
            if self._metrics:
                self._metrics.incr(f"ws.{self._channel}.connect")

            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self._stale_timeout)
                except asyncio.TimeoutError as exc:
                    raise ConnectionError(f"{self._channel} stale for {self._stale_timeout}s") from exc
                self.last_message_ts = now_s()
                if self._metrics:
                    self._metrics.incr(f"ws.{self._channel}.message")
                await self._dispatch(raw)
            self.connected = False

    async def _dispatch(self, raw: str | bytes) -> None:
        text = raw.decode() if isinstance(raw, bytes) else raw
        if not text or text in ("PONG", "PING"):
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self._log.debug("non-json ws frame", extra={"channel": self._channel, "raw": text[:120]})
            return
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            try:
                await self._on_message(event)
            except Exception as exc:  # noqa: BLE001 - one bad event must not kill the socket
                self._log.error("ws handler error", extra={"channel": self._channel,
                                                            "error": repr(exc)}, exc_info=True)

    def stop(self) -> None:
        self._stop.set()


def market_subscription(asset_ids: list[str]) -> dict[str, Any]:
    """Subscription payload for the public ``market`` (order-book) channel."""
    return {"type": "market", "assets_ids": asset_ids}


def user_subscription(markets: list[str], api_key: str, secret: str, passphrase: str) -> dict[str, Any]:
    """Subscription payload for the authenticated ``user`` channel."""
    return {
        "type": "user",
        "markets": markets,
        "auth": {"apiKey": api_key, "secret": secret, "passphrase": passphrase},
    }
