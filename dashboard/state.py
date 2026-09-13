"""Application state: owns every collector, runs the background loops, and
assembles the snapshot that is pushed to browsers once per second.

The 1 Hz broadcast is deliberately *lean* — latest values, no order-book depth,
no indicator series. Heavy detail (full books, indicator history, order history,
analytics) is fetched on demand through REST when a panel needs it. That keeps
the steady-state cost of an idle open tab close to nothing on a 2 vCPU box.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from dashboard import __version__
from dashboard.config import DashboardSettings
from dashboard.control import ControlPlane
from dashboard.db import DashboardDB, EngineReader
from dashboard.engine_view import EngineView
from dashboard.hub import Hub
from dashboard.journal import TradeJournal
from dashboard.live_sync import LiveAccountSync
from dashboard.market_feed import MarketFeed
from dashboard.notifications import NotificationCenter
from dashboard.security import SecurityManager
from dashboard.vps import VpsMonitor
from polybot.config.paths import dashboard_paths, mode_paths
from polybot.utils.time_utils import now_s, utcnow_iso

log = logging.getLogger("polybot.dashboard.state")

MODES = ("paper", "live")


class AppState:
    """Single owner of every long-lived dashboard object."""

    def __init__(self, settings: DashboardSettings) -> None:
        self.settings = settings
        self.started_ts = now_s()
        paths = dashboard_paths().ensure()

        self.db = DashboardDB(settings.db_path)
        self.security = SecurityManager(
            self.db, session_ttl_s=settings.session_ttl_s,
            idle_timeout_s=settings.session_idle_timeout_s,
            max_attempts=settings.login_max_attempts, lockout_s=settings.login_lockout_s)
        self.hub = Hub()
        self.readers: dict[str, EngineReader] = {
            mode: EngineReader(mode, mode_paths(mode).db) for mode in MODES
        }
        self.views: dict[str, EngineView] = {
            mode: EngineView(mode, self.readers[mode], settings) for mode in MODES
        }
        self.market_feed = MarketFeed(settings, self.db)
        self.live_sync = LiveAccountSync(settings)
        self.vps = VpsMonitor(settings)
        self.notifications = NotificationCenter(settings, self.db, self.hub)
        self.journal = TradeJournal(self.db, paths.uploads)
        self.control = ControlPlane(settings, self.db, self.readers)

        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._snapshot: dict[str, Any] = {}
        self._service_cache: dict[str, dict[str, Any]] = {}
        self.broadcasts = 0
        self.bootstrap_password: str | None = None

    # ======================================================================
    # Lifecycle
    # ======================================================================
    async def start(self) -> None:
        self.bootstrap_password = self.security.bootstrap_admin(
            self.settings.bootstrap_user, self.settings.bootstrap_password)
        if self.bootstrap_password:
            log.warning("=" * 70)
            log.warning("INITIAL ADMIN CREATED — username %r, password: %s",
                        self.settings.bootstrap_user, self.bootstrap_password)
            log.warning("Change it on first login. This is printed once, here only.")
            log.warning("=" * 70)

        await self.live_sync.start()
        self._tasks = [
            asyncio.create_task(self.vps.run(), name="vps"),
            asyncio.create_task(self.market_feed.discovery_loop(), name="discovery"),
            asyncio.create_task(self.market_feed.book_loop(), name="books"),
            asyncio.create_task(self.market_feed.candle_loop(), name="candles"),
            asyncio.create_task(self._broadcast_loop(), name="broadcast"),
            asyncio.create_task(self._alert_loop(), name="alerts"),
            asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
        ]
        if self.settings.live_sync_enabled:
            self._tasks.append(asyncio.create_task(self.live_sync.run(), name="live-sync"))
        log.info("dashboard state started", extra={"tasks": len(self._tasks),
                                                    "version": __version__})

    async def stop(self) -> None:
        self._stop.set()
        self.vps.stop()
        self.market_feed.stop()
        self.live_sync.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.market_feed.aclose()
        await self.live_sync.aclose()
        for reader in self.readers.values():
            reader.close()
        self.db.close()
        log.info("dashboard state stopped")

    # ======================================================================
    # Background loops
    # ======================================================================
    async def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                snapshot = await asyncio.to_thread(self.build_snapshot, True)
                self._snapshot = snapshot
                if self.hub.client_count:
                    await self.hub.broadcast("snapshot", {"data": snapshot})
                self.broadcasts += 1
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.error("broadcast failed", extra={"error": repr(exc)}, exc_info=True)
            elapsed = time.perf_counter() - started
            await self._sleep(max(0.2, self.settings.broadcast_interval_s - elapsed))

    async def _alert_loop(self) -> None:
        """Evaluate alert rules and drain engine events, slower than the broadcast."""
        while not self._stop.is_set():
            try:
                if self._snapshot:
                    await self.notifications.evaluate(self._snapshot)
                for mode in MODES:
                    await self.notifications.drain_engine_events(mode, self.readers[mode])
            except Exception as exc:  # noqa: BLE001
                log.error("alert loop failed", extra={"error": repr(exc)})
            await self._sleep(5.0)

    async def _housekeeping_loop(self) -> None:
        """Journal import and session purge every 30 s; table pruning hourly."""
        ticks = 0
        while not self._stop.is_set():
            try:
                for mode in MODES:
                    await asyncio.to_thread(self.journal.sync, mode, self.readers[mode])
                self.security.purge_expired()
            except Exception as exc:  # noqa: BLE001
                log.warning("housekeeping failed", extra={"error": repr(exc)})

            if ticks % 120 == 0:  # 120 x 30 s = once an hour
                try:
                    await asyncio.to_thread(self.db.prune,
                                            candle_keep=self.settings.candle_retention)
                except Exception as exc:  # noqa: BLE001
                    log.warning("prune failed", extra={"error": repr(exc)})
            ticks += 1
            await self._sleep(30.0)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ======================================================================
    # Snapshot
    # ======================================================================
    def build_snapshot(self, light: bool = True) -> dict[str, Any]:
        """Assemble the full application state. Runs off the event loop."""
        vps = self.vps.snapshot()
        services = {service["name"]: service for service in vps.get("services", [])}
        self._service_cache = services

        engines = {}
        for mode in MODES:
            try:
                engines[mode] = self.views[mode].summary(services.get(mode))
            except Exception as exc:  # noqa: BLE001 - one bad engine must not blank the page
                log.warning("engine summary failed", extra={"mode": mode, "error": repr(exc)})
                engines[mode] = {"mode": mode, "available": False, "error": repr(exc)}

        markets = self.market_feed.snapshot(with_indicators=not light, with_depth=not light)
        if light:
            markets["markets"] = [_light_market(row) for row in markets["markets"]]

        return {
            "ts": now_s(),
            "iso": utcnow_iso(),
            "version": __version__,
            "uptime_s": round(now_s() - self.started_ts, 1),
            "engines": engines,
            "live_account": self.live_sync.snapshot(),
            "markets": markets,
            "vps": vps,
            "notifications": {
                "unread": self.notifications.unread_count(),
                "recent": self.notifications.recent(limit=15),
                "channels": self.notifications.channel_status(),
            },
            "dashboard": {
                "clients": self.hub.client_count,
                "broadcasts": self.broadcasts,
                "messages_sent": self.hub.messages_sent,
                "db_bytes": self.db.size_bytes(),
                "control_enabled": self.settings.control_enabled,
                "live_sync_enabled": self.settings.live_sync_enabled,
            },
        }

    @property
    def snapshot(self) -> dict[str, Any]:
        """The most recent broadcast snapshot (built on demand if none yet)."""
        if not self._snapshot:
            self._snapshot = self.build_snapshot(True)
        return self._snapshot

    def service_state(self, mode: str) -> dict[str, Any]:
        return self._service_cache.get(mode, {})


def _light_market(row: dict[str, Any]) -> dict[str, Any]:
    """Strip depth ladders and indicator series from a market row."""
    trimmed = {k: v for k, v in row.items() if k not in ("indicators",)}
    for side in ("up", "down"):
        quote = dict(trimmed.get(side) or {})
        quote.pop("bids", None)
        quote.pop("asks", None)
        trimmed[side] = quote
    return trimmed
