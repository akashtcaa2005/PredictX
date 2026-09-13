"""WebSocket fan-out hub.

One writer task builds a snapshot; the hub pushes it to every connected browser.
Clients that cannot keep up are disconnected rather than allowed to grow an
unbounded send buffer — on a 2 vCPU box a single stalled browser must not be
able to inflate the dashboard's memory footprint.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.hub")

#: Frames allowed to queue per client before it is considered stalled.
_MAX_PENDING = 8


@dataclass
class Client:
    """One connected browser socket."""

    ws: Any
    username: str
    role: str
    sid: str
    connected_ts: float = field(default_factory=now_s)
    channels: set[str] = field(default_factory=lambda: {"snapshot", "notifications"})
    pending: int = 0
    dropped: int = 0

    @property
    def info(self) -> dict[str, Any]:
        return {"username": self.username, "role": self.role,
                "connected_s": round(now_s() - self.connected_ts, 1),
                "channels": sorted(self.channels), "dropped": self.dropped}


class Hub:
    """Tracks connected clients and broadcasts messages to them."""

    def __init__(self) -> None:
        self._clients: dict[int, Client] = {}
        self._lock = asyncio.Lock()
        self._seq = 0
        self.messages_sent = 0

    # ------------------------------------------------------------------
    async def register(self, ws: Any, *, username: str, role: str, sid: str) -> Client:
        client = Client(ws=ws, username=username, role=role, sid=sid)
        async with self._lock:
            self._clients[id(ws)] = client
        log.info("dashboard client connected", extra={"user": username, "clients": len(self._clients)})
        return client

    async def unregister(self, ws: Any) -> None:
        async with self._lock:
            client = self._clients.pop(id(ws), None)
        if client is not None:
            log.info("dashboard client disconnected",
                     extra={"user": client.username, "clients": len(self._clients)})

    async def revoke_session(self, sid: str) -> None:
        """Close every socket belonging to a session that was just revoked."""
        async with self._lock:
            targets = [c for c in self._clients.values() if c.sid == sid]
        for client in targets:
            await self._close(client, code=4401, reason="session revoked")

    # ------------------------------------------------------------------
    @property
    def client_count(self) -> int:
        return len(self._clients)

    def clients(self) -> list[dict[str, Any]]:
        return [c.info for c in self._clients.values()]

    # ------------------------------------------------------------------
    async def broadcast(self, channel: str, payload: dict[str, Any]) -> None:
        """Send ``payload`` to every client subscribed to ``channel``."""
        async with self._lock:
            targets = [c for c in self._clients.values() if channel in c.channels]
        if not targets:
            return
        self._seq += 1
        message = json.dumps({"channel": channel, "seq": self._seq, "ts": now_s(),
                              **payload}, default=str)
        await asyncio.gather(*(self._send(client, message) for client in targets),
                             return_exceptions=True)

    async def send_to(self, client: Client, channel: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"channel": channel, "ts": now_s(), **payload}, default=str)
        await self._send(client, message)

    async def _send(self, client: Client, message: str) -> None:
        if client.pending >= _MAX_PENDING:
            client.dropped += 1
            log.warning("dropping stalled dashboard client", extra={"user": client.username})
            await self._close(client, code=1013, reason="client too slow")
            return
        client.pending += 1
        try:
            await client.ws.send_text(message)
            self.messages_sent += 1
        except Exception as exc:  # noqa: BLE001 - any send failure means the socket is gone
            log.debug("websocket send failed", extra={"user": client.username, "error": repr(exc)})
            await self.unregister(client.ws)
        finally:
            client.pending -= 1

    async def _close(self, client: Client, *, code: int, reason: str) -> None:
        await self.unregister(client.ws)
        try:
            await client.ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - already closed
            pass
