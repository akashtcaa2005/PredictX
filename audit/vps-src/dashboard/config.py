"""Dashboard configuration, loaded from ``env.dashboard.txt`` + the environment.

Nothing is hardcoded and no secret ever has a default: a missing session secret
is a hard failure rather than a silently weak key.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path

from polybot.config.paths import DATA_ROOT, REPO_ROOT, dashboard_paths, mode_paths
from polybot.config.settings import ConfigError, load_env_file

_SECRET_FIELDS = {
    "secret_key", "smtp_password", "telegram_token", "discord_webhook",
    "bootstrap_password",
}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value.strip() != "" else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(float(_get(name, str(default))))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _get_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _get(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.replace("\n", ",").split(",") if item.strip())


@dataclass
class DashboardSettings:
    """Every knob the dashboard service exposes."""

    # -- http ------------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8787
    behind_proxy: bool = True
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    allowed_origins: tuple[str, ...] = ()
    public_url: str = ""

    # -- security --------------------------------------------------------------
    secret_key: str = ""
    session_ttl_s: int = 12 * 3600
    session_idle_timeout_s: int = 2 * 3600
    cookie_secure: bool = True
    cookie_name: str = "polybot_sid"
    login_max_attempts: int = 6
    login_lockout_s: int = 300
    bootstrap_user: str = "admin"
    bootstrap_password: str | None = None

    # -- engines ---------------------------------------------------------------
    paper_unit: str = "polybot-paper.service"
    live_unit: str = "polybot-live.service"
    dashboard_unit: str = "polybot-dashboard.service"
    systemctl_bin: str = "/usr/bin/systemctl"
    use_sudo: bool = True
    control_enabled: bool = True

    # -- live account sync -----------------------------------------------------
    live_sync_enabled: bool = True
    live_sync_interval_s: float = 3.0
    live_slow_sync_interval_s: float = 30.0
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws"
    http_timeout: float = 10.0

    # -- market data -----------------------------------------------------------
    market_assets: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP")
    market_durations: tuple[str, ...] = ("5m", "15m")
    market_discovery_interval_s: float = 60.0
    market_book_interval_s: float = 1.0
    candle_interval_s: int = 15
    candle_retention: int = 5_000
    max_markets_tracked: int = 24

    # -- broadcast -------------------------------------------------------------
    broadcast_interval_s: float = 1.0
    vps_interval_s: float = 5.0
    history_points: int = 600

    # -- alert thresholds ------------------------------------------------------
    alert_cpu_pct: float = 85.0
    alert_memory_pct: float = 88.0
    alert_disk_pct: float = 90.0
    alert_low_balance: float = 5.0
    alert_ws_stale_s: float = 60.0
    alert_engine_stale_s: float = 60.0
    # The compiled strategy does not publish take-profit / stop-loss levels, so
    # these thresholds define what the dashboard calls a "profit target hit" and a
    # "stop loss hit": the realised PnL of a completed round trip.
    alert_profit_target: float = 2.0
    alert_stop_loss: float = 2.0
    alert_cooldown_s: float = 300.0

    # -- notification channels -------------------------------------------------
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    email_from: str = ""
    email_to: tuple[str, ...] = ()

    telegram_enabled: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""

    discord_enabled: bool = False
    discord_webhook: str = ""

    notify_min_severity: str = "warning"

    # -- paths -----------------------------------------------------------------
    repo_root: Path = field(default_factory=lambda: REPO_ROOT)
    data_root: Path = field(default_factory=lambda: DATA_ROOT)
    db_path: Path = field(default_factory=lambda: dashboard_paths().db)
    backup_dir: Path = field(default_factory=lambda: dashboard_paths().backups)
    export_dir: Path = field(default_factory=lambda: dashboard_paths().backups)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "DashboardSettings":
        paths = dashboard_paths().ensure()
        load_env_file(env_file if env_file is not None else paths.env_file)

        secret = _get("DASHBOARD_SECRET_KEY") or ""
        settings = cls(
            host=_get("DASHBOARD_HOST", "127.0.0.1"),  # type: ignore[arg-type]
            port=_get_int("DASHBOARD_PORT", 8787),
            behind_proxy=_get_bool("DASHBOARD_BEHIND_PROXY", True),
            tls_certfile=_get("DASHBOARD_TLS_CERT"),
            tls_keyfile=_get("DASHBOARD_TLS_KEY"),
            allowed_origins=_get_list("DASHBOARD_ALLOWED_ORIGINS"),
            public_url=_get("DASHBOARD_PUBLIC_URL", "") or "",
            secret_key=secret,
            session_ttl_s=_get_int("DASHBOARD_SESSION_TTL_S", 12 * 3600),
            session_idle_timeout_s=_get_int("DASHBOARD_SESSION_IDLE_S", 2 * 3600),
            cookie_secure=_get_bool("DASHBOARD_COOKIE_SECURE", True),
            login_max_attempts=_get_int("DASHBOARD_LOGIN_MAX_ATTEMPTS", 6),
            login_lockout_s=_get_int("DASHBOARD_LOGIN_LOCKOUT_S", 300),
            bootstrap_user=_get("DASHBOARD_ADMIN_USER", "admin"),  # type: ignore[arg-type]
            bootstrap_password=_get("DASHBOARD_ADMIN_PASSWORD"),
            paper_unit=_get("PAPER_UNIT", "polybot-paper.service"),  # type: ignore[arg-type]
            live_unit=_get("LIVE_UNIT", "polybot-live.service"),  # type: ignore[arg-type]
            dashboard_unit=_get("DASHBOARD_UNIT", "polybot-dashboard.service"),  # type: ignore[arg-type]
            systemctl_bin=_get("SYSTEMCTL_BIN", "/usr/bin/systemctl"),  # type: ignore[arg-type]
            use_sudo=_get_bool("DASHBOARD_USE_SUDO", True),
            control_enabled=_get_bool("DASHBOARD_CONTROL_ENABLED", True),
            live_sync_enabled=_get_bool("DASHBOARD_LIVE_SYNC", True),
            live_sync_interval_s=_get_float("LIVE_SYNC_INTERVAL_S", 3.0),
            live_slow_sync_interval_s=_get_float("LIVE_SLOW_SYNC_INTERVAL_S", 30.0),
            clob_host=_get("CLOB_HOST", "https://clob.polymarket.com"),  # type: ignore[arg-type]
            gamma_host=_get("GAMMA_HOST", "https://gamma-api.polymarket.com"),  # type: ignore[arg-type]
            data_api_host=_get("DATA_API_HOST", "https://data-api.polymarket.com"),  # type: ignore[arg-type]
            clob_ws_url=_get("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws"),  # type: ignore[arg-type]
            http_timeout=_get_float("HTTP_TIMEOUT", 10.0),
            market_assets=_get_list("DASHBOARD_ASSETS", ("BTC", "ETH", "SOL", "XRP")),
            market_durations=_get_list("DASHBOARD_DURATIONS", ("5m", "15m")),
            market_discovery_interval_s=_get_float("MARKET_DISCOVERY_INTERVAL_S", 60.0),
            market_book_interval_s=_get_float("MARKET_BOOK_INTERVAL_S", 1.0),
            candle_interval_s=_get_int("CANDLE_INTERVAL_S", 15),
            candle_retention=_get_int("CANDLE_RETENTION", 5_000),
            max_markets_tracked=_get_int("MAX_MARKETS_TRACKED", 24),
            broadcast_interval_s=_get_float("BROADCAST_INTERVAL_S", 1.0),
            vps_interval_s=_get_float("VPS_INTERVAL_S", 5.0),
            history_points=_get_int("HISTORY_POINTS", 600),
            alert_cpu_pct=_get_float("ALERT_CPU_PCT", 85.0),
            alert_memory_pct=_get_float("ALERT_MEMORY_PCT", 88.0),
            alert_disk_pct=_get_float("ALERT_DISK_PCT", 90.0),
            alert_low_balance=_get_float("ALERT_LOW_BALANCE", 5.0),
            alert_ws_stale_s=_get_float("ALERT_WS_STALE_S", 60.0),
            alert_engine_stale_s=_get_float("ALERT_ENGINE_STALE_S", 60.0),
            alert_profit_target=_get_float("ALERT_PROFIT_TARGET", 2.0),
            alert_stop_loss=_get_float("ALERT_STOP_LOSS", 2.0),
            alert_cooldown_s=_get_float("ALERT_COOLDOWN_S", 300.0),
            email_enabled=_get_bool("EMAIL_ENABLED", False),
            smtp_host=_get("SMTP_HOST", "") or "",
            smtp_port=_get_int("SMTP_PORT", 587),
            smtp_user=_get("SMTP_USER", "") or "",
            smtp_password=_get("SMTP_PASSWORD", "") or "",
            smtp_starttls=_get_bool("SMTP_STARTTLS", True),
            email_from=_get("EMAIL_FROM", "") or "",
            email_to=_get_list("EMAIL_TO"),
            telegram_enabled=_get_bool("TELEGRAM_ENABLED", False),
            telegram_token=_get("TELEGRAM_BOT_TOKEN", "") or "",
            telegram_chat_id=_get("TELEGRAM_CHAT_ID", "") or "",
            discord_enabled=_get_bool("DISCORD_ENABLED", False),
            discord_webhook=_get("DISCORD_WEBHOOK_URL", "") or "",
            notify_min_severity=(_get("NOTIFY_MIN_SEVERITY", "warning") or "warning").lower(),
        )
        settings.validate()
        return settings

    # ------------------------------------------------------------------
    def validate(self) -> None:
        if not self.secret_key:
            raise ConfigError(
                "DASHBOARD_SECRET_KEY is required. Generate one with:\n"
                "    python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
                "and put it in env.dashboard.txt")
        if len(self.secret_key) < 32:
            raise ConfigError("DASHBOARD_SECRET_KEY must be at least 32 characters")
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"DASHBOARD_PORT out of range: {self.port}")
        if self.tls_certfile and not self.tls_keyfile:
            raise ConfigError("DASHBOARD_TLS_CERT set without DASHBOARD_TLS_KEY")
        if self.notify_min_severity not in ("info", "warning", "critical"):
            raise ConfigError("NOTIFY_MIN_SEVERITY must be info | warning | critical")
        if self.email_enabled and not (self.smtp_host and self.email_from and self.email_to):
            raise ConfigError("EMAIL_ENABLED requires SMTP_HOST, EMAIL_FROM and EMAIL_TO")
        if self.telegram_enabled and not (self.telegram_token and self.telegram_chat_id):
            raise ConfigError("TELEGRAM_ENABLED requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        if self.discord_enabled and not self.discord_webhook:
            raise ConfigError("DISCORD_ENABLED requires DISCORD_WEBHOOK_URL")

    # ------------------------------------------------------------------
    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_certfile and self.tls_keyfile)

    def unit_for(self, mode: str) -> str:
        return self.live_unit if mode == "live" else self.paper_unit

    def engine_db(self, mode: str) -> Path:
        return mode_paths(mode).db

    def engine_logs(self, mode: str) -> Path:
        return mode_paths(mode).logs

    def engine_state(self, mode: str) -> Path:
        return mode_paths(mode).state

    def redacted(self) -> dict[str, object]:
        """Config with every secret masked — safe to send to the browser."""
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _SECRET_FIELDS and value:
                value = f"set ({len(str(value))} chars)"
            out[f.name] = str(value) if isinstance(value, Path) else value
        return out


def generate_secret() -> str:
    """A fresh 48-byte URL-safe session secret."""
    return secrets.token_urlsafe(48)
