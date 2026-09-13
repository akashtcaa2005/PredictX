"""Configuration loading.

Every value is read from the environment (``env.txt`` is loaded into
``os.environ`` on startup). Nothing is hardcoded: API keys, wallet address,
private key, endpoints and limits all come from config. :meth:`Settings.load`
must run *before* the compiled strategy is imported, because the strategy
framework (:mod:`polybot.strategy.base`) reads ``MAX_TRADE_VALUE`` etc. from the
environment at import time.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from polybot.config.paths import REPO_ROOT, mode_paths

DEFAULT_ENV_FILE = REPO_ROOT / "env.txt"

_SECRET_KEYS = {
    "private_key",
    "api_secret",
    "api_passphrase",
    "builder_secret",
    "builder_passphrase",
    "camber_api_token",
    "api_key",
    "builder_api_key",
}


def load_env_file(path: str | Path = DEFAULT_ENV_FILE, *, override: bool = False) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file into ``os.environ``.

    Blank lines and ``#`` comments are ignored. Existing environment variables
    are preserved unless ``override`` is set, so real env vars win over the file.
    Returns the mapping that was parsed from the file.
    """
    path = Path(path)
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not key:
            continue
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


#: Variables that grant access to real funds. The paper engine deletes these from
#: its own process environment at startup, so even a misconfigured unit file or an
#: inherited shell export cannot give paper trading a route to the live wallet.
WALLET_SECRET_ENV = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_PROXY_ADDRESS",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    "POLY_BUILDER_API_KEY",
    "POLY_BUILDER_SECRET",
    "POLY_BUILDER_PASSPHRASE",
)


def scrub_wallet_secrets() -> list[str]:
    """Remove every wallet credential from ``os.environ``; returns what was dropped."""
    removed = [name for name in WALLET_SECRET_ENV if os.environ.pop(name, None) is not None]
    # Live trading is meaningless without credentials; make the refusal explicit.
    os.environ["ENABLE_LIVE_TRADING"] = "false"
    return removed


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val is not None and val.strip() != "" else default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(float(_get(name, str(default))))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    val = _get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class Settings:
    """Typed, validated view of all runtime configuration."""

    # -- credentials & identity ------------------------------------------------
    private_key: str = ""
    proxy_address: str = ""
    signature_type: int = 1
    chain_id: int = 137

    # explicit L2 CLOB trading creds (optional; derived from the key if absent)
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None

    # builder-program creds (optional; wired into the SDK's builder_config)
    builder_api_key: str | None = None
    builder_secret: str | None = None
    builder_passphrase: str | None = None

    # optional external API token (env-only; never hardcoded)
    camber_api_token: str | None = None
    camber_api_url: str | None = None

    # -- endpoints -------------------------------------------------------------
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws"

    # -- trading control -------------------------------------------------------
    enable_live_trading: bool = False
    markets: tuple[str, ...] = ()
    # Rotating-market discovery (opt-in): when both are non-empty, the engine
    # continuously discovers and trades currently-open crypto up/down markets
    # matching these assets/durations, in addition to any static `markets`.
    # Empty (the default) means discovery never runs -- zero behavior change.
    trade_assets: tuple[str, ...] = ()
    trade_durations: tuple[str, ...] = ()
    trade_discovery_interval_s: float = 60.0
    # Crypto up/down windows are listed a full day ahead of their actual
    # resolving window, so an unbounded discovery pass can match hundreds of
    # them at once. Cap how many the engine registers per cycle, keeping the
    # soonest-to-resolve (mirrors dashboard/market_feed.py's max_markets_tracked).
    max_discovery_markets: int = 6
    tick_interval_s: float = 1.0
    order_poll_interval_s: float = 5.0
    heartbeat_interval_s: float = 15.0
    book_refresh_interval_s: float = 10.0
    cancel_open_orders_on_shutdown: bool = True

    # -- order sizing (also consumed by the compiled strategy framework) -------
    max_trade_value: float = 5.0
    per_order_notional: float = 5.0
    min_order_shares: float = 5.0

    # -- risk limits -----------------------------------------------------------
    max_position_size: float = 1000.0
    max_daily_loss: float = 50.0
    max_concurrent_trades: int = 10
    max_open_orders: int = 50
    max_total_exposure: float = 100.0
    max_order_value: float = 5.0
    cooldown_seconds: float = 0.0
    min_wallet_balance: float = 1.0
    # Minimum USDC allowance (human units) the collateral must have granted to the
    # exchange contracts before live buys are allowed. Defaults to max_total_exposure.
    min_allowance: float = 100.0
    dedup_window_s: float = 5.0

    # -- infra / resilience ----------------------------------------------------
    retry_max_attempts: int = 5
    retry_base_delay: float = 0.5
    retry_max_delay: float = 15.0
    http_timeout: float = 10.0
    ws_ping_interval: float = 10.0
    ws_reconnect_max_backoff: float = 30.0
    ws_stale_timeout: float = 45.0

    # -- logging / state -------------------------------------------------------
    mode: str = "live"                 # 'live' | 'paper' — selects the isolated data tree
    log_dir: Path = field(default_factory=lambda: mode_paths("live").logs)
    log_level: str = "INFO"
    log_json: bool = False
    state_dir: Path = field(default_factory=lambda: mode_paths("live").state)
    kill_switch_file: Path = field(default_factory=lambda: mode_paths("live").kill_switch)
    db_path: Path = field(default_factory=lambda: mode_paths("live").db)
    telemetry_interval_s: float = 2.0
    equity_sample_interval_s: float = 15.0

    # -- paper trading (ignored in live mode) ---------------------------------
    paper_starting_cash: float = 1000.0
    paper_fill_latency_ms: float = 250.0
    paper_slippage_ticks: float = 0.0
    paper_fee_bps: float = 0.0
    paper_maker_fill_ratio: float = 1.0

    @property
    def dry_run(self) -> bool:
        """True unless live trading is explicitly enabled (safe default)."""
        return not self.enable_live_trading

    @classmethod
    def load(cls, env_file: str | Path | None = None, *, mode: str = "live") -> "Settings":
        """Load config from ``env_file`` + the process environment and validate it.

        ``mode`` selects the isolated data tree (``data/paper`` vs ``data/live``)
        and the default env file. Explicit ``LOG_DIR`` / ``STATE_DIR`` /
        ``ENGINE_DB`` environment variables still win.
        """
        paths = mode_paths(mode).ensure()
        load_env_file(env_file if env_file is not None else paths.env_file)
        if mode == "paper":
            scrub_wallet_secrets()

        max_trade_value = _get_float("MAX_TRADE_VALUE", 5.0)
        max_total_exposure = _get_float("MAX_TOTAL_EXPOSURE", 100.0)
        markets_raw = _get("MARKETS", "") or ""
        markets = tuple(m.strip() for m in markets_raw.replace("\n", ",").split(",") if m.strip())
        trade_assets_raw = _get("TRADE_ASSETS", "") or ""
        trade_assets = tuple(a.strip().upper() for a in trade_assets_raw.split(",") if a.strip())
        trade_durations_raw = _get("TRADE_DURATIONS", "") or ""
        trade_durations = tuple(d.strip().lower() for d in trade_durations_raw.split(",") if d.strip())

        log_dir = Path(_get("LOG_DIR", str(paths.logs)))  # type: ignore[arg-type]
        state_dir = Path(_get("STATE_DIR", str(paths.state)))  # type: ignore[arg-type]

        settings = cls(
            private_key=_get("POLYMARKET_PRIVATE_KEY", "") or "",
            proxy_address=_get("POLYMARKET_PROXY_ADDRESS", "") or "",
            signature_type=_get_int("SIGNATURE_TYPE", 1),
            chain_id=_get_int("CHAIN_ID", 137),
            api_key=_get("POLY_API_KEY"),
            api_secret=_get("POLY_API_SECRET"),
            api_passphrase=_get("POLY_API_PASSPHRASE"),
            builder_api_key=_get("POLY_BUILDER_API_KEY"),
            builder_secret=_get("POLY_BUILDER_SECRET"),
            builder_passphrase=_get("POLY_BUILDER_PASSPHRASE"),
            camber_api_token=_get("CAMBER_API_TOKEN"),
            camber_api_url=_get("CAMBER_API_URL"),
            clob_host=_get("CLOB_HOST", "https://clob.polymarket.com"),  # type: ignore[arg-type]
            gamma_host=_get("GAMMA_HOST", "https://gamma-api.polymarket.com"),  # type: ignore[arg-type]
            data_api_host=_get("DATA_API_HOST", "https://data-api.polymarket.com"),  # type: ignore[arg-type]
            clob_ws_url=_get("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws"),  # type: ignore[arg-type]
            enable_live_trading=_get_bool("ENABLE_LIVE_TRADING", False),
            markets=markets,
            trade_assets=trade_assets,
            trade_durations=trade_durations,
            trade_discovery_interval_s=_get_float("TRADE_DISCOVERY_INTERVAL_S", 60.0),
            max_discovery_markets=_get_int("MAX_DISCOVERY_MARKETS", 6),
            tick_interval_s=_get_float("TICK_INTERVAL_S", 1.0),
            order_poll_interval_s=_get_float("ORDER_POLL_INTERVAL_S", 5.0),
            heartbeat_interval_s=_get_float("HEARTBEAT_INTERVAL_S", 15.0),
            book_refresh_interval_s=_get_float("BOOK_REFRESH_INTERVAL_S", 10.0),
            cancel_open_orders_on_shutdown=_get_bool("CANCEL_OPEN_ORDERS_ON_SHUTDOWN", True),
            max_trade_value=max_trade_value,
            per_order_notional=_get_float("PER_ORDER_NOTIONAL", max_trade_value / 3.0),
            min_order_shares=_get_float("MIN_ORDER_SHARES", 5.0),
            max_position_size=_get_float("MAX_POSITION_SIZE", 1000.0),
            max_daily_loss=_get_float("MAX_DAILY_LOSS", 50.0),
            max_concurrent_trades=_get_int("MAX_CONCURRENT_TRADES", 10),
            max_open_orders=_get_int("MAX_OPEN_ORDERS", 50),
            max_total_exposure=max_total_exposure,
            max_order_value=_get_float("MAX_ORDER_VALUE", max_trade_value),
            cooldown_seconds=_get_float("COOLDOWN_SECONDS", 0.0),
            min_wallet_balance=_get_float("MIN_WALLET_BALANCE", 1.0),
            min_allowance=_get_float("MIN_ALLOWANCE", max_total_exposure),
            dedup_window_s=_get_float("DEDUP_WINDOW_S", 5.0),
            retry_max_attempts=_get_int("RETRY_MAX_ATTEMPTS", 5),
            retry_base_delay=_get_float("RETRY_BASE_DELAY", 0.5),
            retry_max_delay=_get_float("RETRY_MAX_DELAY", 15.0),
            http_timeout=_get_float("HTTP_TIMEOUT", 10.0),
            ws_ping_interval=_get_float("WS_PING_INTERVAL", 10.0),
            ws_reconnect_max_backoff=_get_float("WS_RECONNECT_MAX_BACKOFF", 30.0),
            ws_stale_timeout=_get_float("WS_STALE_TIMEOUT", 45.0),
            mode=mode,
            log_dir=log_dir,
            log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
            log_json=_get_bool("LOG_JSON", False),
            state_dir=state_dir,
            kill_switch_file=Path(_get("KILL_SWITCH_FILE", str(state_dir / "KILL_SWITCH"))),  # type: ignore[arg-type]
            db_path=Path(_get("ENGINE_DB", str(paths.db))),  # type: ignore[arg-type]
            telemetry_interval_s=_get_float("TELEMETRY_INTERVAL_S", 2.0),
            equity_sample_interval_s=_get_float("EQUITY_SAMPLE_INTERVAL_S", 15.0),
            paper_starting_cash=_get_float("PAPER_STARTING_CASH", 1000.0),
            paper_fill_latency_ms=_get_float("PAPER_FILL_LATENCY_MS", 250.0),
            paper_slippage_ticks=_get_float("PAPER_SLIPPAGE_TICKS", 0.0),
            paper_fee_bps=_get_float("PAPER_FEE_BPS", 0.0),
            paper_maker_fill_ratio=_get_float("PAPER_MAKER_FILL_RATIO", 1.0),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Raise :class:`ConfigError` for statically-detectable misconfiguration."""
        if self.mode == "paper":
            # Paper trading never authenticates a wallet, so key checks do not apply.
            if self.max_order_value <= 0 or self.max_trade_value <= 0:
                raise ConfigError("max_order_value and max_trade_value must be > 0")
            if self.paper_starting_cash <= 0:
                raise ConfigError("PAPER_STARTING_CASH must be > 0")
            return
        if not self.private_key:
            raise ConfigError("POLYMARKET_PRIVATE_KEY is required")
        pk = self.private_key[2:] if self.private_key.startswith("0x") else self.private_key
        if len(pk) != 64 or any(c not in "0123456789abcdefABCDEF" for c in pk):
            raise ConfigError("POLYMARKET_PRIVATE_KEY must be a 32-byte hex string")
        if self.signature_type not in (0, 1, 2, 3):
            raise ConfigError(f"SIGNATURE_TYPE must be 0/1/2/3, got {self.signature_type}")
        if self.signature_type in (1, 2, 3) and not self.proxy_address:
            raise ConfigError(
                f"SIGNATURE_TYPE={self.signature_type} requires POLYMARKET_PROXY_ADDRESS (funder)"
            )
        if self.max_order_value <= 0 or self.max_trade_value <= 0:
            raise ConfigError("max_order_value and max_trade_value must be > 0")

    def redacted(self) -> dict[str, object]:
        """Config as a dict with secrets masked, safe to log."""
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _SECRET_KEYS and value:
                value = _mask(str(value))
            out[f.name] = str(value) if isinstance(value, Path) else value
        return out


def _mask(secret: str) -> str:
    """Mask a secret for logs: reveal nothing reversible (length + one-way fp)."""
    fp = hashlib.sha256(secret.encode()).hexdigest()[:8]
    return f"****(len={len(secret)}, sha256={fp})"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""
