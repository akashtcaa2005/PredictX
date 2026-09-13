"""Alerting: rule evaluation, persistence and multi-channel delivery.

Alerts are produced in two ways:

* **Pushed** — the trading engines write rows into their ``events`` table as
  things happen (fills, rejections, risk halts). The dashboard tails that table.
* **Derived** — rules evaluated against the current snapshot each tick (low
  balance, stale websocket, high CPU, engine offline…).

Every alert is deduplicated by ``(kind, mode)`` within a cooldown window, so a
condition that persists for an hour produces one notification, not thousands.
Delivery failures on one channel never block the others, and never block the
dashboard's own loop.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from dashboard.config import DashboardSettings
from dashboard.db import DashboardDB
from dashboard.hub import Hub
from dashboard.security import mask_secrets
from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.notifications")

SEVERITIES = ("info", "warning", "critical")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

#: Engine event kinds mapped to the alert they raise.
_ENGINE_EVENT_ALERTS: dict[str, tuple[str, str]] = {
    "fill": ("info", "Trade executed"),
    "order_rejected": ("warning", "Order rejected"),
    "order_failed": ("critical", "Order submission failed"),
    "risk_halt": ("critical", "Risk halt engaged"),
    "startup_failed": ("critical", "Engine failed to start"),
    "shutdown": ("warning", "Engine shutting down"),
    "startup": ("info", "Engine started"),
    "cancel_all": ("warning", "All open orders cancelled"),
}


class NotificationCenter:
    """Owns alert rules, the notification store and channel delivery."""

    def __init__(self, settings: DashboardSettings, db: DashboardDB, hub: Hub) -> None:
        self._s = settings
        self._db = db
        self._hub = hub
        self._last_fired: dict[tuple[str, str], float] = {}
        self._seen_event_id: dict[str, int] = {"paper": 0, "live": 0}
        self._seen_trade_id: dict[str, int] = {"paper": 0, "live": 0}
        self._primed: set[str] = set()
        self.delivery_errors: dict[str, str] = {}
        self.sent_counts: dict[str, int] = {"browser": 0, "email": 0, "telegram": 0, "discord": 0}

    # ======================================================================
    # Emission
    # ======================================================================
    async def emit(self, severity: str, kind: str, title: str, body: str = "", *,
                   mode: str = "system", cooldown: float | None = None,
                   force: bool = False) -> bool:
        """Record and deliver one alert. Returns False when suppressed."""
        if severity not in SEVERITIES:
            severity = "info"
        key = (kind, mode)
        window = self._s.alert_cooldown_s if cooldown is None else cooldown
        if not force and window > 0:
            last = self._last_fired.get(key, 0.0)
            if now_s() - last < window:
                return False
        self._last_fired[key] = now_s()

        body = mask_secrets(body or "")
        notification_id = self._db.execute(
            "INSERT INTO notifications (ts, severity, kind, title, body, mode, read, delivered) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, '')",
            (now_s(), severity, kind, title, body, mode))

        payload = {"id": notification_id, "ts": now_s(), "severity": severity, "kind": kind,
                   "title": title, "body": body, "mode": mode, "read": 0}
        await self._hub.broadcast("notifications", {"notification": payload})
        self.sent_counts["browser"] += 1

        if _SEVERITY_RANK[severity] >= _SEVERITY_RANK[self._s.notify_min_severity]:
            delivered = await self._deliver(severity, title, body, mode)
            if delivered:
                self._db.execute("UPDATE notifications SET delivered = ? WHERE id = ?",
                                 (",".join(delivered), notification_id))
        return True

    # ======================================================================
    # Rule evaluation
    # ======================================================================
    async def evaluate(self, snapshot: dict[str, Any]) -> None:
        """Run every derived rule against the current snapshot."""
        try:
            await self._evaluate_system(snapshot.get("vps") or {})
            await self._evaluate_live(snapshot.get("live_account") or {})
            await self._evaluate_markets(snapshot.get("markets") or {})
            for mode in ("paper", "live"):
                await self._evaluate_engine(mode, (snapshot.get("engines") or {}).get(mode) or {})
        except Exception as exc:  # noqa: BLE001 - alerting must never break the tick
            log.warning("alert evaluation failed", extra={"error": repr(exc)})

    async def _evaluate_system(self, vps: dict[str, Any]) -> None:
        cpu = (vps.get("cpu") or {}).get("percent")
        if cpu is not None and cpu >= self._s.alert_cpu_pct:
            await self.emit("warning", "high_cpu", "High CPU usage",
                            f"CPU at {cpu:.0f}% (threshold {self._s.alert_cpu_pct:.0f}%)")
        memory = (vps.get("memory") or {}).get("percent")
        if memory is not None and memory >= self._s.alert_memory_pct:
            await self.emit("warning", "high_memory", "High memory usage",
                            f"Memory at {memory:.0f}% (threshold {self._s.alert_memory_pct:.0f}%)")
        disk = (vps.get("disk") or {}).get("percent")
        if disk is not None and disk >= self._s.alert_disk_pct:
            await self.emit("warning", "high_disk", "Disk nearly full",
                            f"Disk at {disk:.0f}% (threshold {self._s.alert_disk_pct:.0f}%)")
        for service in vps.get("services") or []:
            if service.get("available") and service.get("failed"):
                await self.emit("critical", f"service_failed_{service['name']}",
                                f"Service failed: {service['unit']}",
                                f"state={service.get('active')} result={service.get('result')}",
                                mode=service["name"])

    async def _evaluate_live(self, live: dict[str, Any]) -> None:
        if not live:
            return
        status = live.get("status") or {}
        wallet = live.get("wallet") or {}

        # A dashboard that cannot reach the account shows empty panels; say why.
        if status.get("enabled") and not status.get("authenticated"):
            await self.emit("critical", "live_sync_down",
                            "Cannot read the live Polymarket account",
                            str(status.get("auth_status") or "not connected"),
                            mode="live", cooldown=900)
        if status.get("authenticated") and not status.get("api_ok"):
            await self.emit("critical", "api_failure", "Polymarket API unreachable",
                            str(status.get("last_error") or "health check failed"), mode="live")
        if status.get("authenticated") and not status.get("websocket_connected"):
            await self.emit("warning", "websocket_disconnect", "Live user websocket disconnected",
                            "falling back to REST polling", mode="live")
        balance = wallet.get("balance")
        if balance is not None and wallet.get("address") and balance < self._s.alert_low_balance:
            await self.emit("warning", "low_balance", "Low wallet balance",
                            f"USDC balance {balance:.2f} is below {self._s.alert_low_balance:.2f}",
                            mode="live")
        if wallet.get("address") and wallet.get("allowances") and not wallet.get("allowance_ok"):
            await self.emit("critical", "allowance_issue", "Insufficient USDC allowance",
                            "Buy orders will be rejected until trading is enabled for this "
                            "wallet in the Polymarket UI", mode="live")

    async def _evaluate_markets(self, markets: dict[str, Any]) -> None:
        discovery = markets.get("discovery") or {}
        if discovery.get("error"):
            await self.emit("warning", "market_discovery_failed",
                            "Market discovery is failing",
                            str(discovery["error"]), mode="system", cooldown=900)
        elif discovery.get("count") and not discovery.get("tracked"):
            await self.emit("warning", "no_markets_tracked",
                            "No matching markets are open",
                            "Discovery succeeded but nothing matched the configured assets "
                            "and durations.", mode="system", cooldown=3600)

    async def _evaluate_engine(self, mode: str, engine: dict[str, Any]) -> None:
        if not engine:
            return
        heartbeat = engine.get("heartbeat") or {}
        age = engine.get("heartbeat_age_s")
        running = engine.get("service", {}).get("healthy")
        if running and age is not None and age > self._s.alert_engine_stale_s:
            await self.emit("critical", "engine_stale", f"{mode.title()} engine is not reporting",
                            f"last heartbeat {age:.0f}s ago", mode=mode)
        risk = heartbeat.get("risk") or {}
        if risk.get("halted"):
            reason = str(risk.get("halt_reason") or "")
            kind = "daily_loss_limit" if "daily loss" in reason.lower() else "risk_halt"
            title = ("Daily loss limit reached" if kind == "daily_loss_limit"
                     else f"{mode.title()} trading halted")
            await self.emit("critical", kind, title, reason, mode=mode)
        errors = ((heartbeat.get("metrics") or {}).get("counters") or {}).get("strategy.errors")
        if errors:
            await self.emit("warning", "strategy_failure", f"{mode.title()} strategy errors",
                            f"{int(errors)} strategy tick error(s) recorded", mode=mode)
        for connection in heartbeat.get("connections") or []:
            if not connection.get("connected"):
                await self.emit("warning", f"engine_ws_{connection.get('channel')}",
                                f"{mode.title()} {connection.get('channel')} socket disconnected",
                                f"reconnects: {connection.get('reconnects', 0)}", mode=mode)

    # ======================================================================
    # Engine event tailing
    # ======================================================================
    async def drain_engine_events(self, mode: str, reader: Any) -> None:
        """Turn newly written engine events and trades into notifications."""
        first_pass = mode not in self._primed
        rows = reader.query(
            "SELECT id, ts, level, kind, message FROM events WHERE id > ? ORDER BY id LIMIT 200",
            (self._seen_event_id[mode],))
        for row in rows:
            self._seen_event_id[mode] = max(self._seen_event_id[mode], int(row["id"]))
            if first_pass:
                continue  # don't replay history as fresh alerts on startup
            mapping = _ENGINE_EVENT_ALERTS.get(row["kind"])
            if mapping is None:
                continue
            severity, title = mapping
            await self.emit(severity, row["kind"], f"{mode.title()}: {title}",
                            str(row["message"]), mode=mode,
                            cooldown=0 if row["kind"] in ("fill", "order_rejected") else None)

        trades = reader.query(
            "SELECT id, market_label, market_id, side, qty, entry_price, exit_price, pnl, "
            "duration_s FROM trades WHERE id > ? ORDER BY id LIMIT 100",
            (self._seen_trade_id[mode],))
        for trade in trades:
            self._seen_trade_id[mode] = max(self._seen_trade_id[mode], int(trade["id"]))
            if first_pass:
                continue
            await self._trade_closed_alert(mode, trade)

        self._primed.add(mode)

    async def _trade_closed_alert(self, mode: str, trade: dict[str, Any]) -> None:
        pnl = float(trade["pnl"])
        label = trade.get("market_label") or trade.get("market_id") or "market"
        body = (f"{trade['side']} {trade['qty']:g} @ {trade['entry_price']:.3f} → "
                f"{trade['exit_price']:.3f} in {label}, PnL {pnl:+.4f}")
        await self.emit("info", "trade_closed", f"{mode.title()}: Trade closed", body,
                        mode=mode, cooldown=0)
        if pnl >= self._s.alert_profit_target:
            await self.emit("info", "profit_target", f"{mode.title()}: Profit target hit",
                            body, mode=mode, cooldown=0)
        elif pnl <= -self._s.alert_stop_loss:
            await self.emit("warning", "stop_loss", f"{mode.title()}: Stop loss hit",
                            body, mode=mode, cooldown=0)

    # ======================================================================
    # Channels
    # ======================================================================
    async def _deliver(self, severity: str, title: str, body: str, mode: str) -> list[str]:
        tasks: list[tuple[str, Any]] = []
        if self._s.email_enabled:
            tasks.append(("email", self._send_email(severity, title, body, mode)))
        if self._s.telegram_enabled:
            tasks.append(("telegram", self._send_telegram(severity, title, body, mode)))
        if self._s.discord_enabled:
            tasks.append(("discord", self._send_discord(severity, title, body, mode)))
        if not tasks:
            return ["browser"]

        results = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        delivered = ["browser"]
        for (channel, _), result in zip(tasks, results):
            if isinstance(result, BaseException):
                self.delivery_errors[channel] = repr(result)
                log.warning("notification delivery failed",
                            extra={"channel": channel, "error": repr(result)})
            else:
                delivered.append(channel)
                self.sent_counts[channel] = self.sent_counts.get(channel, 0) + 1
                self.delivery_errors.pop(channel, None)
        return delivered

    async def _send_email(self, severity: str, title: str, body: str, mode: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"[polybot/{mode}][{severity}] {title}"
        message["From"] = self._s.email_from
        message["To"] = ", ".join(self._s.email_to)
        message.set_content(f"{title}\n\n{body}\n\nmode: {mode}\nseverity: {severity}\n")
        await asyncio.to_thread(self._smtp_send, message)

    def _smtp_send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=15) as server:
            if self._s.smtp_starttls:
                server.starttls()
            if self._s.smtp_user:
                server.login(self._s.smtp_user, self._s.smtp_password)
            server.send_message(message)

    async def _send_telegram(self, severity: str, title: str, body: str, mode: str) -> None:
        icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}[severity]
        text = f"{icon} *{_md_escape(title)}*\n{_md_escape(body)}\n_mode: {mode}_"
        url = f"https://api.telegram.org/bot{self._s.telegram_token}/sendMessage"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json={
                "chat_id": self._s.telegram_chat_id, "text": text, "parse_mode": "Markdown"})
            response.raise_for_status()

    async def _send_discord(self, severity: str, title: str, body: str, mode: str) -> None:
        colour = {"info": 0x3BA55D, "warning": 0xE9A23B, "critical": 0xED4245}[severity]
        payload = {"embeds": [{"title": title, "description": body or "—", "color": colour,
                               "footer": {"text": f"polybot · {mode} · {severity}"}}]}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(self._s.discord_webhook, json=payload)
            response.raise_for_status()

    # ======================================================================
    # Store access
    # ======================================================================
    def recent(self, limit: int = 100, *, unread_only: bool = False,
               mode: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if unread_only:
            clauses.append("read = 0")
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._db.query(
            f"SELECT * FROM notifications {where} ORDER BY ts DESC LIMIT ?", params)

    def unread_count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM notifications WHERE read = 0")
        return int(row["n"]) if row else 0

    def mark_read(self, notification_id: int | None = None) -> int:
        if notification_id is None:
            return self._db.execute("UPDATE notifications SET read = 1 WHERE read = 0")
        return self._db.execute("UPDATE notifications SET read = 1 WHERE id = ?",
                                (notification_id,))

    def channel_status(self) -> dict[str, Any]:
        return {
            "browser": {"enabled": True, "sent": self.sent_counts["browser"]},
            "email": {"enabled": self._s.email_enabled, "sent": self.sent_counts["email"],
                      "error": self.delivery_errors.get("email"),
                      "to": list(self._s.email_to)},
            "telegram": {"enabled": self._s.telegram_enabled, "sent": self.sent_counts["telegram"],
                         "error": self.delivery_errors.get("telegram")},
            "discord": {"enabled": self._s.discord_enabled, "sent": self.sent_counts["discord"],
                        "error": self.delivery_errors.get("discord")},
            "min_severity": self._s.notify_min_severity,
            "cooldown_s": self._s.alert_cooldown_s,
        }

    async def test_channels(self) -> dict[str, Any]:
        """Send a test alert through every configured channel and report results."""
        delivered = await self._deliver("info", "polybot test notification",
                                        "If you can read this, the channel works.", "system")
        return {"delivered": delivered, "errors": dict(self.delivery_errors)}


def _md_escape(text: str) -> str:
    for char in ("_", "*", "[", "]", "`"):
        text = text.replace(char, f"\\{char}")
    return text
