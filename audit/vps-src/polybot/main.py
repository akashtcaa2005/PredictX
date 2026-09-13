"""Live trading bot orchestrator.

Wires every component together, runs the startup validation sequence
(environment → credentials → wallet → API → strategy → market connectivity),
supervises the async tasks, and shuts down gracefully. Trading is **dry-run by
default**; real orders are only submitted when ``ENABLE_LIVE_TRADING=true``.
"""
from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

from polybot.api.camber import CamberClient
from polybot.api.rest_client import RestClient
from polybot.config.settings import ConfigError, Settings
from polybot.exchange.auth import build_clob_client
from polybot.exchange.clob import ClobExchange
from polybot.exchange.market_registry import MarketRegistry
from polybot.exchange.websocket import ClobWebSocket, market_subscription, user_subscription
from polybot.execution.executor import Broker
from polybot.execution.strategy_runner import StrategyRunner
from polybot.logging.loggers import execution_logger, get_logger, setup_logging
from polybot.monitoring.health import HealthMonitor
from polybot.monitoring.metrics import Metrics
from polybot.orders.manager import OrderManager
from polybot.portfolio.manager import PortfolioManager
from polybot.risk.engine import RiskEngine
from polybot.telemetry.publisher import TelemetryPublisher
from polybot.telemetry.recorder import EngineRecorder
from polybot.utils.retry import RetryPolicy
from polybot.utils.time_utils import now_s, utcnow_iso
from polybot.wallet.allowance import AllowanceError, AllowanceManager
from polybot.wallet.balance import BalanceManager
from polybot.wallet.maker import MakerWalletError, MakerWalletValidator


class StartupError(RuntimeError):
    """Raised when a startup validation step fails; the bot must not trade."""


class TradingBot:
    """Owns the full component graph and lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.log = execution_logger()
        self.metrics = Metrics()
        self._retry = RetryPolicy(attempts=settings.retry_max_attempts,
                                  base_delay=settings.retry_base_delay,
                                  max_delay=settings.retry_max_delay)
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._guard_tripped = False

        # populated during startup()
        self.auth: Any = None
        self.exchange: ClobExchange | None = None
        self.registry: MarketRegistry | None = None
        self.balance: BalanceManager | None = None
        self.allowance: AllowanceManager | None = None
        self.maker: MakerWalletValidator | None = None
        self.approval: Any = None
        self.readiness: Any = None
        self.portfolio: PortfolioManager | None = None
        self.risk: RiskEngine | None = None
        self.orders: OrderManager | None = None
        self.broker: Broker | None = None
        self.strategy: Any = None
        self.runner: StrategyRunner | None = None
        self.health: HealthMonitor | None = None
        self.market_ws: ClobWebSocket | None = None
        self.user_ws: ClobWebSocket | None = None
        self._data_api: RestClient | None = None
        self._camber: CamberClient | None = None
        self.recorder = EngineRecorder(settings.db_path, mode=settings.mode,
                                       logger=get_logger("telemetry"))
        self.publisher: TelemetryPublisher | None = None

    # ======================================================================
    # Startup validation (Steps 12 & 14)
    # ======================================================================
    async def startup(self) -> None:
        s = self.s
        self.log.info("=" * 68)
        self.log.info("Polymarket live trading bot — starting",
                      extra={"mode": "LIVE" if s.enable_live_trading else "DRY-RUN"})
        self.log.info("config", extra={"config": s.redacted()})
        self.recorder.start()
        self.recorder.record_config(s.redacted())
        self.recorder.record_event(
            "info", "startup",
            f"engine starting in {'LIVE' if s.enable_live_trading else 'DRY-RUN'} mode")
        if s.enable_live_trading:
            self.log.warning("LIVE TRADING ENABLED — real orders will be submitted with real funds")
        else:
            self.log.info("DRY-RUN — orders are simulated; set ENABLE_LIVE_TRADING=true to go live")

        # 1) environment + credentials (Settings.validate already ran in load())
        self.log.info("[1/6] environment & credentials validated")

        # 2) authenticate + build exchange client, verify API access
        try:
            self.auth = await asyncio.to_thread(build_clob_client, s, get_logger("exchange.auth"))
        except Exception as exc:  # noqa: BLE001
            raise StartupError(f"authentication failed: {exc}") from exc
        self.exchange = ClobExchange(self.auth.client, signature_type=s.signature_type,
                                     retry_policy=self._retry, logger=get_logger("exchange.clob"),
                                     metrics=self.metrics)
        if not await self.exchange.health():
            raise StartupError("CLOB API health check failed (get_ok)")
        self.log.info("[2/6] API access OK", extra={"address": self.auth.address,
                      "creds": self.auth.creds_source, "builder": self.auth.builder_enabled})

        # 2b) maker / deposit-wallet authorization: confirm the wallet the bot will
        # sign every order for (maker = funder/deposit wallet) is coherent and that
        # the L2 creds belong to the same account. In live mode a failure aborts
        # startup before any order is signed.
        self.maker = MakerWalletValidator(
            self.exchange, signer=self.auth.address, funder=self.auth.funder,
            signature_type=s.signature_type, api_key=self.auth.api_key,
            enable_live_trading=s.enable_live_trading, logger=get_logger("wallet.maker"))
        try:
            maker_info = await self.maker.validate()
        except MakerWalletError as exc:
            raise StartupError(f"maker/deposit-wallet not authorized: {exc}") from exc
        self.log.info("[2b/6] maker/deposit wallet checked",
                      extra={"maker": maker_info.get("maker_address"),
                             "authorized": maker_info.get("authorized")})

        # 3) wallet balance
        self.balance = BalanceManager(self.exchange, min_balance=s.min_wallet_balance,
                                      refresh_interval=s.heartbeat_interval_s, logger=get_logger("wallet"))
        try:
            bal = await self.balance.validate()
            self.log.info("[3/6] wallet balance OK", extra={"balance": bal})
        except Exception as exc:  # noqa: BLE001
            if s.enable_live_trading:
                raise StartupError(f"wallet balance validation failed: {exc}") from exc
            self.log.warning("[3/6] wallet balance below minimum (allowed in dry-run)",
                             extra={"error": repr(exc)})

        # 3b) collateral (USDC) allowance — buys are rejected by the CLOB if the
        # exchanges are not approved. The bot only verifies (proxy approvals are
        # granted gaslessly via the Polymarket UI); in live mode an insufficient
        # allowance aborts startup so no buy order is ever placed at 0 allowance.
        # 3b-i) optional automatic USDC approve (EOA wallets only; opt-in). For
        # proxy/deposit wallets this correctly refuses and points to the gasless
        # Polymarket flow. Runs before verify so the allowance is set first.
        if s.auto_approve_allowance:
            from polybot.wallet.approval import ApprovalError, ApprovalManager

            self.approval = ApprovalManager(
                self.exchange, settings=s, signer=self.auth.address,
                funder=self.auth.funder, signature_type=s.signature_type,
                logger=get_logger("wallet.approval"))
            try:
                approve_result = await self.approval.ensure_allowance()
                self.log.info("USDC auto-approve step", extra={"result": approve_result})
            except ApprovalError as exc:
                if s.enable_live_trading:
                    raise StartupError(f"USDC allowance approval failed: {exc}") from exc
                self.log.warning("USDC auto-approve not performed (allowed in dry-run)",
                                 extra={"reason": str(exc)})

        self.allowance = AllowanceManager(
            self.exchange, signature_type=s.signature_type, chain_id=s.chain_id,
            min_allowance=s.min_allowance, enable_live_trading=s.enable_live_trading,
            funder=self.auth.funder, logger=get_logger("wallet"))
        try:
            allowance_ok = await self.allowance.verify()
        except AllowanceError as exc:
            raise StartupError(f"collateral allowance not set: {exc}") from exc
        self.log.info("[3b/6] collateral allowance checked",
                      extra={"sufficient": allowance_ok})

        # 3c) consolidated trading-readiness report (auth + maker/deposit wallet +
        # allowance + balance) -> state/readiness.json. Read-only reporter; live
        # trading is still enforced/blocked by the gates above. Names exactly which
        # requirement is missing and points to the gasless "Enable Trading" flow.
        from polybot.wallet.readiness import TradingReadiness

        self.readiness = TradingReadiness(
            self.exchange, signer=self.auth.address, funder=self.auth.funder,
            signature_type=s.signature_type, api_key=self.auth.api_key, chain_id=s.chain_id,
            min_allowance=s.min_allowance, min_balance=s.min_wallet_balance,
            logger=get_logger("wallet.readiness"), readiness_file=s.state_dir / "readiness.json")
        try:
            readiness = await self.readiness.evaluate()
            self.log.info("[3c/6] trading readiness",
                          extra={"live_ready": readiness.get("live_ready"),
                                 "missing": readiness.get("missing_requirements")})
        except Exception as exc:  # noqa: BLE001 - reporter must never break startup
            self.log.warning("readiness evaluation failed (non-fatal)", extra={"error": repr(exc)})

        # 4) load the compiled strategy (imported here so env is loaded first)
        from polybot.strategy.loader import load_strategy_class

        try:
            strategy_cls = load_strategy_class()
        except Exception as exc:  # noqa: BLE001
            raise StartupError(f"failed to load compiled strategy: {exc}") from exc
        self.log.info("[4/6] strategy loaded", extra={"class": strategy_cls.__name__,
                      "strategy": getattr(strategy_cls, "name", "?")})

        # 5) market connectivity
        if not s.markets:
            raise StartupError("no markets configured — set MARKETS=<condition_id>[,<condition_id>...]")
        self.registry = MarketRegistry(self.exchange, logger=get_logger("exchange.registry"))
        for condition_id in s.markets:
            try:
                await self.registry.register(condition_id)
                await self.registry.refresh_book(condition_id)
            except Exception as exc:  # noqa: BLE001
                raise StartupError(f"market connectivity failed for {condition_id}: {exc}") from exc
        for condition_id in s.markets:
            info = self.registry.info(condition_id)
            if info is not None:
                self.recorder.register_market_label(
                    condition_id, f"{info.up_outcome}/{info.down_outcome}")
        self.log.info("[5/6] market connectivity OK", extra={"markets": list(s.markets),
                      "tokens": len(self.registry.all_token_ids())})

        # 6) wire the trading components
        self._wire_components(strategy_cls)
        await self.portfolio.seed_from_data_api()
        recovered = await self.orders.recover()
        self.log.info("[6/6] components wired", extra={"recovered_orders": recovered,
                      "strategy": self.strategy.name})

        self.log.info("startup complete — %s mode", "LIVE" if s.enable_live_trading else "DRY-RUN")

    def _wire_components(self, strategy_cls: type) -> None:
        s = self.s
        assert self.exchange and self.registry and self.balance
        self._data_api = RestClient(s.data_api_host, timeout=s.http_timeout, retry_policy=self._retry,
                                    logger=get_logger("api"), metrics=self.metrics, name="data_api")
        self._camber = CamberClient.from_settings(s, metrics=self.metrics, logger=get_logger("api"))
        if self._camber:
            self.log.info("Camber API integration enabled (token from env)")

        self.portfolio = PortfolioManager(self.registry, funder_address=self.auth.funder,
                                          data_api=self._data_api, logger=get_logger("portfolio"))
        self.risk = RiskEngine(s, portfolio=self.portfolio, balance=self.balance,
                               logger=get_logger("risk"), metrics=self.metrics)
        self.orders = OrderManager(self.exchange, self.registry, self.risk, self.portfolio,
                                   dry_run=s.dry_run, dedup_window_s=s.dedup_window_s,
                                   logger=get_logger("orders"), trade_logger=get_logger("trade"),
                                   metrics=self.metrics, recorder=self.recorder)
        self.risk.bind_orders(self.orders)
        self.orders.bind_loop(asyncio.get_running_loop())

        self.broker = Broker(self.orders, self.registry, self.portfolio,
                             logger=get_logger("execution"), metrics=self.metrics,
                             on_signal=lambda m, sig: self.publisher and self.publisher.note_signal(m, sig))
        # portfolio=None => strategy.notional_in_market() delegates to broker, which
        # combines open-order notional + position notional in one place.
        self.strategy = strategy_cls(broker=self.broker, risk=self.risk, portfolio=None)
        self.runner = StrategyRunner(self.strategy, self.registry, tick_interval_s=s.tick_interval_s,
                                     logger=get_logger("strategy"), metrics=self.metrics)

        self.market_ws = ClobWebSocket(
            s.clob_ws_url, channel="market",
            subscribe_message=market_subscription(self.registry.all_token_ids()),
            on_message=self._on_market_event, logger=get_logger("websocket"), metrics=self.metrics,
            ping_interval=s.ws_ping_interval, stale_timeout=s.ws_stale_timeout,
            max_backoff=s.ws_reconnect_max_backoff)
        self.user_ws = ClobWebSocket(
            s.clob_ws_url, channel="user",
            subscribe_message=user_subscription(list(s.markets), self.auth.api_key,
                                                 self.auth.api_secret, self.auth.api_passphrase),
            on_message=self._on_user_event, logger=get_logger("websocket"), metrics=self.metrics,
            ping_interval=s.ws_ping_interval, stale_timeout=s.ws_stale_timeout,
            max_backoff=s.ws_reconnect_max_backoff)

        self.health = HealthMonitor(address=self.auth.address, balance=self.balance,
                                    portfolio=self.portfolio, orders=self.orders, risk=self.risk,
                                    sockets=[self.market_ws, self.user_ws], strategy_runner=self.runner,
                                    metrics=self.metrics, interval_s=s.heartbeat_interval_s,
                                    status_file=s.state_dir / "status.json",
                                    logger=get_logger("execution"))

        self.publisher = TelemetryPublisher(
            recorder=self.recorder, registry=self.registry, portfolio=self.portfolio,
            orders=self.orders, risk=self.risk, strategy=self.strategy, runner=self.runner,
            metrics=self.metrics, sockets=[self.market_ws, self.user_ws], mode=s.mode,
            cash_provider=lambda: self.balance.balance if self.balance else 0.0,
            wallet_address=self.auth.address, funder_address=self.auth.funder,
            interval_s=s.telemetry_interval_s, equity_interval_s=s.equity_sample_interval_s,
            logger=get_logger("telemetry"))

    # ======================================================================
    # WebSocket dispatchers
    # ======================================================================
    async def _on_market_event(self, event: dict[str, Any]) -> None:
        asset_id = event.get("asset_id") or event.get("assetId")
        if not asset_id or self.registry is None:
            return
        bids = event.get("bids")
        asks = event.get("asks")
        if bids is not None or asks is not None:
            self.registry.apply_book(str(asset_id), bids=bids or [], asks=asks or [], ts=now_s())

    async def _on_user_event(self, event: dict[str, Any]) -> None:
        if self.orders is not None:
            await self.orders.on_user_event(event)

    # ======================================================================
    # Run / supervise
    # ======================================================================
    async def run(self) -> None:
        assert self.orders and self.runner and self.market_ws and self.user_ws and self.health
        self._install_signal_handlers()
        self._tasks = [
            asyncio.create_task(self.publisher.run(), name="telemetry"),
            asyncio.create_task(self.orders.run(), name="orders"),
            asyncio.create_task(self._poll_loop(), name="order-poll"),
            asyncio.create_task(self._book_refresh_loop(), name="book-refresh"),
            asyncio.create_task(self.market_ws.run(), name="ws-market"),
            asyncio.create_task(self.user_ws.run(), name="ws-user"),
            asyncio.create_task(self.runner.run(), name="strategy"),
            asyncio.create_task(self.health.heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._guard_loop(), name="risk-guard"),
        ]
        self.log.info("bot running (%d tasks) — Ctrl-C to stop", len(self._tasks))
        await self._shutdown.wait()
        await self.shutdown()

    async def _poll_loop(self) -> None:
        assert self.orders
        while not self._shutdown.is_set():
            await self.orders.poll()
            await self._sleep(self.s.order_poll_interval_s)

    async def _book_refresh_loop(self) -> None:
        assert self.registry
        while not self._shutdown.is_set():
            for market_id in list(self.s.markets):
                try:
                    await self.registry.refresh_book(market_id)
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("book refresh failed", extra={"market": market_id, "error": repr(exc)})
            await self._sleep(self.s.book_refresh_interval_s)

    async def _guard_loop(self) -> None:
        """React to the kill switch / emergency stop by pulling all open orders."""
        assert self.risk and self.orders
        while not self._shutdown.is_set():
            if self.risk.is_halted() and not self._guard_tripped:
                self._guard_tripped = True
                self.log.critical("risk halt active — cancelling all open orders",
                                  extra={"reason": self.risk.halt_reason})
                self.recorder.record_event("critical", "risk_halt",
                                           f"risk halt engaged: {self.risk.halt_reason}")
                await self.orders.cancel_all()
            elif not self.risk.is_halted():
                self._guard_tripped = False
            await self._sleep(2.0)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ======================================================================
    # Shutdown (Step 13)
    # ======================================================================
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown, sig.name)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - non-posix
                pass

    def request_shutdown(self, reason: str = "signal") -> None:
        if not self._shutdown.is_set():
            self.log.warning("shutdown requested", extra={"reason": reason})
            self._shutdown.set()

    async def shutdown(self) -> None:
        self.log.info("graceful shutdown starting")
        self.recorder.record_event("warning", "shutdown", "engine shutting down")
        for component in (self.runner, self.health, self.market_ws, self.user_ws, self.orders,
                          self.publisher):
            if component is not None:
                component.stop()

        if self.orders and self.s.cancel_open_orders_on_shutdown:
            try:
                await self.orders.cancel_all()
            except Exception as exc:  # noqa: BLE001
                self.log.error("cancel_all on shutdown failed", extra={"error": repr(exc)})

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        for closer in (self._data_api, self._camber):
            if closer is not None:
                try:
                    await closer.aclose()
                except Exception:  # noqa: BLE001
                    pass

        self._save_state()
        self.recorder.stop()
        self.log.info("shutdown complete")

    def _save_state(self) -> None:
        try:
            self.s.state_dir.mkdir(parents=True, exist_ok=True)
            state = {
                "ts": utcnow_iso(),
                "orders": self.orders.snapshot() if self.orders else {},
                "portfolio": self.portfolio.snapshot() if self.portfolio else {},
                "risk": self.risk.status() if self.risk else {},
                "metrics": self.metrics.snapshot(),
            }
            (self.s.state_dir / "last_state.json").write_text(json.dumps(state, indent=2, default=str))
            self.log.info("state saved", extra={"path": str(self.s.state_dir / "last_state.json")})
        except Exception as exc:  # noqa: BLE001
            self.log.error("failed to save state", extra={"error": repr(exc)})


async def main() -> int:
    """Entry point: load config, set up logging, validate, and run."""
    settings = Settings.load()
    setup_logging(settings.log_dir, settings.log_level, settings.log_json)
    log = execution_logger()
    bot = TradingBot(settings)
    try:
        await bot.startup()
    except (StartupError, ConfigError) as exc:
        log.critical("STARTUP ABORTED", extra={"error": str(exc)})
        bot.recorder.record_event("critical", "startup_failed", str(exc))
        bot.recorder.stop()
        return 2
    except Exception as exc:  # noqa: BLE001
        log.critical("STARTUP ABORTED (unexpected)", extra={"error": repr(exc)}, exc_info=True)
        bot.recorder.record_event("critical", "startup_failed", repr(exc))
        bot.recorder.stop()
        return 2
    try:
        await bot.run()
    except asyncio.CancelledError:  # pragma: no cover
        pass
    except Exception as exc:  # noqa: BLE001
        log.critical("fatal error while running", extra={"error": repr(exc)}, exc_info=True)
        bot.request_shutdown("fatal")
        await bot.shutdown()
        return 1
    return 0
