"""Paper trading engine — an independent process with its own everything.

Same strategy, same broker, same order manager, same risk engine as live; a
simulated exchange and a virtual wallet underneath. It reads its config from
``env.paper.txt``, writes to ``data/paper/`` and holds no wallet credentials at
all: :func:`polybot.config.settings.scrub_wallet_secrets` deletes them from the
process environment before anything else runs.
"""
from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

from polybot.api.rest_client import RestClient
from polybot.config.settings import ConfigError, Settings, scrub_wallet_secrets
from polybot.exchange.clob import ClobExchange
from polybot.exchange.market_discovery import discover_eligible_markets
from polybot.exchange.market_registry import MarketRegistry
from polybot.exchange.websocket import ClobWebSocket, market_subscription
from polybot.execution.executor import Broker
from polybot.execution.strategy_runner import StrategyRunner
from polybot.logging.loggers import execution_logger, get_logger, setup_logging
from polybot.orders.manager import OrderManager
from polybot.paper.account import PaperAccount, PaperBalanceManager
from polybot.paper.exchange import PaperExchange
from polybot.portfolio.manager import PortfolioManager
from polybot.risk.engine import RiskEngine
from polybot.monitoring.metrics import Metrics
from polybot.telemetry.publisher import TelemetryPublisher
from polybot.telemetry.recorder import EngineRecorder
from polybot.telemetry.schema import connect
from polybot.utils.retry import RetryPolicy
from polybot.utils.time_utils import now_s, utcnow_iso


class PaperStartupError(RuntimeError):
    """Raised when the paper engine cannot reach a usable starting state."""


def build_public_reader(settings: Settings, metrics: Metrics, retry: RetryPolicy) -> ClobExchange:
    """A keyless CLOB client for public market data only.

    Constructed with no private key and no API credentials, so it is physically
    incapable of signing or submitting an order.

    Both SDK generations are accepted: ``py-clob-client-v2`` is preferred when
    present (that is what the migrated live engine uses), falling back to the v1
    package. The public read surface is identical across both.
    """
    client_cls = None
    for module_name in ("py_clob_client_v2.client", "py_clob_client.client"):
        try:
            module = __import__(module_name, fromlist=["ClobClient"])
        except ImportError:
            continue
        client_cls = getattr(module, "ClobClient", None)
        if client_cls is not None:
            break
    if client_cls is None:  # pragma: no cover - environment dependent
        raise ConfigError("no py-clob-client installed; run `pip install -r requirements.txt`")

    client = client_cls(host=settings.clob_host, chain_id=settings.chain_id)
    return ClobExchange(client, signature_type=0, retry_policy=retry,
                        logger=get_logger("paper.reader"), metrics=metrics)


class PaperTradingBot:
    """Owns the paper engine's component graph and lifecycle."""

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

        self.recorder = EngineRecorder(settings.db_path, mode="paper",
                                       logger=get_logger("telemetry"))
        self.account = PaperAccount(settings.paper_starting_cash,
                                    state_file=settings.state_dir / "paper_account.json",
                                    logger=get_logger("paper.account"))
        self.reader: ClobExchange | None = None
        self.exchange: PaperExchange | None = None
        self.registry: MarketRegistry | None = None
        self.balance: PaperBalanceManager | None = None
        self.portfolio: PortfolioManager | None = None
        self.risk: RiskEngine | None = None
        self.orders: OrderManager | None = None
        self.broker: Broker | None = None
        self.strategy: Any = None
        self.runner: StrategyRunner | None = None
        self.market_ws: ClobWebSocket | None = None
        self.publisher: TelemetryPublisher | None = None
        # Rotating-market discovery (opt-in, see Settings.trade_assets/trade_durations).
        self._gamma: RestClient | None = None
        self._market_ws_task: asyncio.Task[Any] | None = None

    # ======================================================================
    # Startup
    # ======================================================================
    async def startup(self) -> None:
        s = self.s
        self.recorder.start()
        self.log.info("=" * 68)
        self.log.info("Polymarket PAPER trading engine — starting")
        self.log.info("config", extra={"config": s.redacted()})
        self.recorder.record_config(s.redacted())

        dropped = scrub_wallet_secrets()
        self.log.info("[1/5] wallet isolation enforced",
                      extra={"scrubbed_env": len(dropped), "live_trading": False})

        # 1) public market data client (no key, no creds, cannot sign)
        self.reader = build_public_reader(s, self.metrics, self._retry)
        if not await self.reader.health():
            raise PaperStartupError("public CLOB API unreachable (get_ok failed)")
        self.log.info("[2/5] public market data OK", extra={"host": s.clob_host})

        # 2) simulated venue + virtual wallet
        self.balance = PaperBalanceManager(self.account, min_balance=0.0,
                                           logger=get_logger("paper.balance"))
        self.registry = MarketRegistry(self.reader, logger=get_logger("exchange.registry"))
        self.exchange = PaperExchange(
            self.reader, self.account, fee_bps=s.paper_fee_bps,
            fill_latency_ms=s.paper_fill_latency_ms, slippage_ticks=s.paper_slippage_ticks,
            maker_fill_ratio=s.paper_maker_fill_ratio,
            inventory_provider=lambda m, side: self.portfolio.position_qty(m, side) if self.portfolio else 0.0,
            logger=get_logger("paper.exchange"), metrics=self.metrics)
        self.log.info("[3/5] simulated exchange ready", extra=self.account.snapshot())

        # 3) strategy
        from polybot.strategy.loader import load_strategy_class

        try:
            strategy_cls = load_strategy_class()
        except Exception as exc:  # noqa: BLE001
            raise PaperStartupError(f"failed to load compiled strategy: {exc}") from exc
        self.log.info("[4/5] strategy loaded", extra={"class": strategy_cls.__name__})

        # 4) markets
        discovery_enabled = bool(s.trade_assets and s.trade_durations)
        if not s.markets and not discovery_enabled:
            raise PaperStartupError(
                "no markets configured — set MARKETS=<condition_id>[,...] in env.paper.txt, "
                "or TRADE_ASSETS=<BTC,ETH,...> + TRADE_DURATIONS=<5m,15m,...>")
        for condition_id in s.markets:
            try:
                await self.registry.register(condition_id)
                await self.registry.refresh_book(condition_id)
            except Exception as exc:  # noqa: BLE001
                raise PaperStartupError(f"market connectivity failed for {condition_id}: {exc}") from exc
            info = self.registry.info(condition_id)
            if info is not None:
                self.recorder.register_market_label(condition_id,
                                                    f"{info.up_outcome}/{info.down_outcome}")
        if discovery_enabled:
            self._gamma = RestClient(s.gamma_host, timeout=s.http_timeout, retry_policy=self._retry,
                                     logger=get_logger("api"), metrics=self.metrics, name="gamma")
            try:
                await self._run_discovery_cycle()
            except Exception as exc:  # noqa: BLE001 - a slow first discovery must not abort startup
                self.log.warning("initial market discovery failed", extra={"error": repr(exc)})
        if not self.registry.all_token_ids():
            raise PaperStartupError("market connectivity failed: no markets registered "
                                    "(static MARKETS empty and discovery found nothing yet)")
        for token_id in self.registry.all_token_ids():
            await self.exchange.refresh_depth(token_id)

        self._wire_components(strategy_cls)
        restored = self._restore_positions()
        self.log.info("[5/5] components wired", extra={
            "static_markets": list(s.markets), "discovery_enabled": discovery_enabled,
            "restored_positions": restored, "strategy": self.strategy.name})
        self.recorder.record_startup({"ts": utcnow_iso(), "mode": "paper",
                                      "markets": list(s.markets),
                                      "starting_cash": s.paper_starting_cash,
                                      "strategy": self.strategy.name})
        self.recorder.record_event("info", "startup", "paper engine started")

    def _wire_components(self, strategy_cls: type) -> None:
        s = self.s
        assert self.registry and self.exchange and self.balance
        self.portfolio = PortfolioManager(self.registry, funder_address="", data_api=None,
                                          logger=get_logger("portfolio"))
        self.risk = RiskEngine(s, portfolio=self.portfolio, balance=self.balance,
                               logger=get_logger("risk"), metrics=self.metrics)
        # dry_run=False: paper orders *are* submitted — to the simulator.
        self.orders = OrderManager(self.exchange, self.registry, self.risk, self.portfolio,
                                   dry_run=False, dedup_window_s=s.dedup_window_s,
                                   logger=get_logger("orders"), trade_logger=get_logger("trade"),
                                   metrics=self.metrics, recorder=self.recorder)
        self.risk.bind_orders(self.orders)
        self.orders.bind_loop(asyncio.get_running_loop())

        self.broker = Broker(self.orders, self.registry, self.portfolio,
                             logger=get_logger("execution"), metrics=self.metrics,
                             on_signal=lambda m, sig: self.publisher and self.publisher.note_signal(m, sig))
        self.strategy = strategy_cls(broker=self.broker, risk=self.risk, portfolio=None)
        self.runner = StrategyRunner(self.strategy, self.registry, tick_interval_s=s.tick_interval_s,
                                     logger=get_logger("strategy"), metrics=self.metrics)

        self.market_ws = ClobWebSocket(
            s.clob_ws_url, channel="market",
            subscribe_message=market_subscription(self.registry.all_token_ids()),
            on_message=self._on_market_event, logger=get_logger("websocket"), metrics=self.metrics,
            ping_interval=s.ws_ping_interval, stale_timeout=s.ws_stale_timeout,
            max_backoff=s.ws_reconnect_max_backoff)

        self.publisher = TelemetryPublisher(
            recorder=self.recorder, registry=self.registry, portfolio=self.portfolio,
            orders=self.orders, risk=self.risk, strategy=self.strategy, runner=self.runner,
            metrics=self.metrics, sockets=[self.market_ws], mode="paper",
            cash_provider=lambda: self.account.cash, wallet_address="paper",
            funder_address="paper", interval_s=s.telemetry_interval_s,
            equity_interval_s=s.equity_sample_interval_s, logger=get_logger("telemetry"))

    def _restore_positions(self) -> int:
        """Reload the last persisted paper positions so restarts are continuous."""
        assert self.portfolio
        if not self.s.db_path.exists():
            return 0
        try:
            conn = connect(self.s.db_path, read_only=True)
            rows = [dict(r) for r in conn.execute(
                "SELECT market_id, side, qty, avg_price, realized_pnl FROM positions WHERE qty > 0")]
            conn.close()
        except Exception as exc:  # noqa: BLE001 - a fresh database is normal
            self.log.debug("no paper positions to restore", extra={"error": repr(exc)})
            return 0
        return self.portfolio.restore(rows)

    # ======================================================================
    # Event handling
    # ======================================================================
    async def _on_market_event(self, event: dict[str, Any]) -> None:
        if self.registry is None or self.exchange is None:
            return
        if event.get("event_type") == "price_change":
            ts = now_s()
            changes = event.get("price_changes")
            for change in changes if isinstance(changes, list) else []:
                if not isinstance(change, dict):
                    continue
                asset_id = change.get("asset_id") or change.get("assetId")
                if not asset_id:
                    continue
                self.registry.apply_price_change(
                    str(asset_id), best_bid=change.get("best_bid"),
                    best_ask=change.get("best_ask"), ts=ts)
            return
        asset_id = event.get("asset_id") or event.get("assetId")
        if not asset_id:
            return
        bids, asks = event.get("bids"), event.get("asks")
        if bids is None and asks is None:
            return
        self.registry.apply_book(str(asset_id), bids=bids or [], asks=asks or [], ts=now_s())
        self.exchange.apply_book(str(asset_id), bids=bids or [], asks=asks or [])

    # ======================================================================
    # Run / supervise
    # ======================================================================
    async def run(self) -> None:
        assert self.orders and self.runner and self.market_ws and self.publisher
        self._install_signal_handlers()
        self._start_market_ws_tasks()
        self._tasks = [
            asyncio.create_task(self.publisher.run(), name="telemetry"),
            asyncio.create_task(self.orders.run(), name="orders"),
            asyncio.create_task(self._match_loop(), name="paper-match"),
            asyncio.create_task(self._book_refresh_loop(), name="book-refresh"),
            asyncio.create_task(self._market_discovery_loop(), name="market-discovery"),
            asyncio.create_task(self.runner.run(), name="strategy"),
            asyncio.create_task(self._guard_loop(), name="risk-guard"),
        ]
        self.log.info("paper engine running (%d tasks)", len(self._tasks) + 1)
        await self._shutdown.wait()
        await self.shutdown()

    async def _match_loop(self) -> None:
        """Match resting paper orders against live depth, then reconcile."""
        assert self.exchange and self.orders
        while not self._shutdown.is_set():
            try:
                fills = await asyncio.to_thread(self.exchange.match)
                if fills:
                    await self.orders.poll()
            except Exception as exc:  # noqa: BLE001 - matching must never die
                self.log.error("paper match pass failed", extra={"error": repr(exc)}, exc_info=True)
            await self._sleep(max(0.2, self.s.tick_interval_s / 2.0))

    async def _book_refresh_loop(self) -> None:
        """REST backstop for depth, in case the websocket feed goes quiet."""
        assert self.registry and self.exchange
        while not self._shutdown.is_set():
            for state in self.registry.all_states():
                try:
                    await self.registry.refresh_book(state.market_id)
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("book refresh failed", extra={"market": state.market_id,
                                                                 "error": repr(exc)})
            for token_id in self.registry.all_token_ids():
                try:
                    await self.exchange.refresh_depth(token_id)
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("depth refresh failed", extra={"token": token_id,
                                                                  "error": repr(exc)})
            await self._sleep(self.s.book_refresh_interval_s)

    # ======================================================================
    # Rotating-market discovery (opt-in: TRADE_ASSETS + TRADE_DURATIONS)
    # ======================================================================
    async def _market_discovery_loop(self) -> None:
        if not (self.s.trade_assets and self.s.trade_durations):
            return  # feature disabled -- exit immediately, no idle polling
        while not self._shutdown.is_set():
            await self._sleep(self.s.trade_discovery_interval_s)
            if self._shutdown.is_set():
                break
            try:
                await self._run_discovery_cycle()
            except Exception as exc:  # noqa: BLE001 - discovery must never take trading down
                self.log.error("market discovery cycle failed", extra={"error": repr(exc)})

    async def _run_discovery_cycle(self) -> None:
        assert self.registry is not None
        if self._gamma is None:
            return
        eligible_all = await discover_eligible_markets(
            self._gamma, assets=self.s.trade_assets, durations=self.s.trade_durations)
        # Windows are listed a day ahead of their actual resolving window, so
        # "eligible" can be large; only take the soonest-to-resolve up to the cap
        # (discover_eligible_markets already sorts soonest-first).
        eligible = eligible_all[: self.s.max_discovery_markets]
        dropped = len(eligible_all) - len(eligible)
        if dropped > 0:
            self.log.debug("discovery: capped eligible markets", extra={
                "eligible": len(eligible_all), "kept": len(eligible), "dropped": dropped})
        eligible_ids = {m.condition_id for m in eligible}
        active_ids = {state.market_id for state in self.registry.all_states()}
        static_ids = set(self.s.markets)  # never auto-retire a statically-configured market

        changed = False
        for market in eligible:
            if market.condition_id in active_ids:
                continue
            try:
                await self.registry.register(market.condition_id)
                await self.registry.refresh_book(market.condition_id)
                if self.exchange is not None:
                    await self.exchange.refresh_depth(market.up_token)
                    await self.exchange.refresh_depth(market.down_token)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("discovery: failed to register market",
                                 extra={"market": market.condition_id, "error": repr(exc)})
                continue
            self.recorder.register_market_tags(market.condition_id, market.asset, market.duration)
            info = self.registry.info(market.condition_id)
            if info is not None:
                self.recorder.register_market_label(market.condition_id,
                                                    f"{info.up_outcome}/{info.down_outcome}")
            self.log.info("discovery: registered market", extra={
                "market": market.condition_id, "asset": market.asset, "duration": market.duration})
            changed = True

        for market_id in active_ids - eligible_ids - static_ids:
            if not self._market_is_safe_to_retire(market_id):
                continue
            self.registry.retire(market_id)
            self.log.info("discovery: retired market", extra={"market": market_id})
            changed = True

        if changed and self.market_ws is not None:
            await self._resubscribe_market_data()

    def _market_is_safe_to_retire(self, market_id: str) -> bool:
        """A market only retires once it can no longer produce a fill/reconcile
        surprise: no open orders, no non-flat position. Never a hard delete
        either way -- see MarketRegistry.retire()."""
        if self.orders is not None and market_id in self.orders.active_markets():
            return False
        if self.portfolio is not None and market_id in self.portfolio.active_markets():
            return False
        return True

    def _start_market_ws_tasks(self) -> None:
        assert self.market_ws is not None
        self._market_ws_task = asyncio.create_task(self.market_ws.run(), name="ws-market")

    async def _stop_market_ws_tasks(self) -> None:
        if self.market_ws is not None:
            self.market_ws.stop()
        if self._market_ws_task is not None:
            self._market_ws_task.cancel()
            await asyncio.gather(self._market_ws_task, return_exceptions=True)
        self._market_ws_task = None

    async def _resubscribe_market_data(self) -> None:
        """Rebuild the market websocket for the current token set.

        No incremental-subscribe protocol is available (the CLOB client only
        ever sends its subscribe payload once per connection), so a rotation
        is a full teardown + reconnect -- mirroring
        dashboard/market_feed.py's _resubscribe(), including the REST
        snapshot immediately afterward so a newly-added market isn't blind
        until its first websocket message arrives.
        """
        assert self.registry is not None
        await self._stop_market_ws_tasks()
        tokens = self.registry.all_token_ids()
        self.market_ws = ClobWebSocket(
            self.s.clob_ws_url, channel="market", subscribe_message=market_subscription(tokens),
            on_message=self._on_market_event, logger=get_logger("websocket"), metrics=self.metrics,
            ping_interval=self.s.ws_ping_interval, stale_timeout=self.s.ws_stale_timeout,
            max_backoff=self.s.ws_reconnect_max_backoff)
        self._start_market_ws_tasks()
        if self.publisher is not None:
            self.publisher.set_sockets([self.market_ws])
        for token_id in tokens:
            try:
                if self.exchange is not None:
                    await self.exchange.refresh_depth(token_id)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("post-resubscribe depth refresh failed",
                               extra={"token": token_id, "error": repr(exc)})
        for state in self.registry.all_states():
            try:
                await self.registry.refresh_book(state.market_id)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("post-resubscribe book refresh failed",
                               extra={"market": state.market_id, "error": repr(exc)})

    async def _guard_loop(self) -> None:
        assert self.risk and self.orders
        while not self._shutdown.is_set():
            if self.risk.is_halted() and not self._guard_tripped:
                self._guard_tripped = True
                self.log.critical("paper risk halt — cancelling open orders",
                                  extra={"reason": self.risk.halt_reason})
                self.recorder.record_event("critical", "risk_halt",
                                           f"paper risk halt: {self.risk.halt_reason}")
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
    # Shutdown
    # ======================================================================
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown, sig.name)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass

    def request_shutdown(self, reason: str = "signal") -> None:
        if not self._shutdown.is_set():
            self.log.warning("shutdown requested", extra={"reason": reason})
            self._shutdown.set()

    async def shutdown(self) -> None:
        self.log.info("paper engine shutting down")
        self.recorder.record_event("warning", "shutdown", "paper engine shutting down")
        for component in (self.runner, self.market_ws, self.orders, self.publisher):
            if component is not None:
                component.stop()
        if self.orders and self.s.cancel_open_orders_on_shutdown:
            try:
                await self.orders.cancel_all()
            except Exception as exc:  # noqa: BLE001
                self.log.error("paper cancel_all failed", extra={"error": repr(exc)})
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._stop_market_ws_tasks()
        if self._gamma is not None:
            try:
                await self._gamma.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._save_state()
        self.recorder.stop()
        self.log.info("paper engine stopped")

    def _save_state(self) -> None:
        try:
            self.s.state_dir.mkdir(parents=True, exist_ok=True)
            state = {
                "ts": utcnow_iso(),
                "account": self.account.snapshot(),
                "orders": self.orders.snapshot() if self.orders else {},
                "portfolio": self.portfolio.snapshot() if self.portfolio else {},
                "risk": self.risk.status() if self.risk else {},
                "metrics": self.metrics.snapshot(),
            }
            (self.s.state_dir / "last_state.json").write_text(json.dumps(state, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001
            self.log.error("failed to save paper state", extra={"error": repr(exc)})


async def main() -> int:
    """Entry point for the paper trading service."""
    settings = Settings.load(mode="paper")
    setup_logging(settings.log_dir, settings.log_level, settings.log_json)
    log = execution_logger()
    bot = PaperTradingBot(settings)
    try:
        await bot.startup()
    except (PaperStartupError, ConfigError) as exc:
        log.critical("PAPER STARTUP ABORTED", extra={"error": str(exc)})
        bot.recorder.record_event("critical", "startup_failed", str(exc))
        bot.recorder.stop()
        return 2
    except Exception as exc:  # noqa: BLE001
        log.critical("PAPER STARTUP ABORTED (unexpected)", extra={"error": repr(exc)}, exc_info=True)
        bot.recorder.record_event("critical", "startup_failed", repr(exc))
        bot.recorder.stop()
        return 2
    try:
        await bot.run()
    except asyncio.CancelledError:  # pragma: no cover
        pass
    except Exception as exc:  # noqa: BLE001
        log.critical("fatal error in paper engine", extra={"error": repr(exc)}, exc_info=True)
        bot.request_shutdown("fatal")
        await bot.shutdown()
        return 1
    return 0
