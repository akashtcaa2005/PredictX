"""Full-orchestration test for :class:`polybot.main.TradingBot` (offline, dry-run).

The live Polymarket API is not reachable from CI, so we inject a fake CLOB client
and drive the real startup sequence, a live-style strategy tick, and graceful
shutdown. This exercises everything the unit tests don't: the startup validation
steps, component wiring, book refresh, the dry-run submission path, and the
state-save on shutdown.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

os.environ.update({
    "ENABLE_LIVE_TRADING": "false",
    "MARKETS": "0xTESTMARKET",
    "RETRY_MAX_ATTEMPTS": "1",
    "HTTP_TIMEOUT": "1",
    "LOG_DIR": "./logs",
    "STATE_DIR": "./state",
    "MAX_TRADE_VALUE": "5",
})

from polybot import main as main_mod  # noqa: E402
from polybot.config.settings import Settings  # noqa: E402
from polybot.exchange.auth import AuthContext  # noqa: E402
from polybot.logging.loggers import setup_logging  # noqa: E402
from polybot.portfolio.manager import PortfolioManager  # noqa: E402

MID = "0xTESTMARKET"


class _FakeBook:
    bids = [SimpleNamespace(price="0.01", size="500")]
    asks = [SimpleNamespace(price="0.02", size="500")]
    tick_size = "0.01"
    min_order_size = 5.0
    neg_risk = False
    last_trade_price = "0.01"


class FakeClient:
    """Minimal stand-in for py_clob_client_v2.ClobClient used in dry-run startup."""

    def get_ok(self): return "OK"
    def get_address(self): return "0xSIGNER"
    def get_server_time(self): return int(time.time())
    def get_tick_size(self, token_id): return "0.01"
    def get_neg_risk(self, token_id): return False
    def get_order_book(self, token_id): return _FakeBook()
    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None): return []
    def get_orders(self, params=None, next_cursor="MA=="): return []  # legacy alias
    def get_balance_allowance(self, params=None):
        # 1000 USDC balance (6dp) + large allowance to all three exchange spenders.
        return {"balance": "1000000000", "allowances": {
            "0xE111180000d2663C0091e4f400237545B87B996B": "1000000000000",
            "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": "1000000000000",
            "0xe2222d279d744050d28e00520010520000310F59": "1000000000000"}}

    def update_balance_allowance(self, params=None): return {"success": True}
    def get_api_keys(self): return {"apiKeys": ["k"]}          # creds belong to signer
    def get_closed_only_mode(self): return {"closed_only": False}  # not banned

    def get_market(self, condition_id):
        end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return {
            "condition_id": condition_id,
            "tokens": [{"token_id": "U", "outcome": "Yes"}, {"token_id": "D", "outcome": "No"}],
            "minimum_tick_size": 0.01,
            "minimum_order_size": 5,
            "neg_risk": False,
            "end_date_iso": end,
            "accepting_orders": True,
        }


def _fake_build_clob_client(settings, logger=None):
    return AuthContext(client=FakeClient(), address="0xSIGNER", funder=settings.proxy_address or "0xFUND",
                       signature_type=settings.signature_type, creds_source="fake", builder_enabled=False,
                       api_key="k", api_secret="s", api_passphrase="p")


async def _noop_seed(self):  # replaces network seeding
    return 0


async def _scenario() -> None:
    setup_logging("./logs", "WARNING")
    settings = Settings.load()
    main_mod.build_clob_client = _fake_build_clob_client            # inject fake auth/client
    PortfolioManager.seed_from_data_api = _noop_seed                # skip network seeding

    bot = main_mod.TradingBot(settings)
    await bot.startup()

    # startup wired everything and validated market connectivity
    assert bot.strategy.name == "1c_buy"
    state = bot.registry.state(MID)
    assert state is not None and state.latest is not None
    assert state.latest.up_ask == 0.02 and state.latest.up_bid == 0.01

    # drive one live-style tick (age the market past ENTRY_DELAY_S so it enters)
    state.open_ts = time.time() - 60
    run_task = asyncio.create_task(bot.orders.run())
    try:
        await asyncio.to_thread(bot.strategy.on_bar, state)
        await asyncio.sleep(0.4)
        assert bot.orders.open_count() == 6, bot.orders.open_count()
        assert all(o.simulated for o in bot.orders.open_orders())
    finally:
        bot.request_shutdown("test")
        await bot.shutdown()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    # graceful shutdown persisted state
    state_file = settings.state_dir / "last_state.json"
    assert state_file.is_file()
    saved = json.loads(state_file.read_text())
    assert saved["orders"]["total"] >= 6


def test_startup_offline():
    asyncio.run(_scenario())


if __name__ == "__main__":
    test_startup_offline()
    print("startup orchestration (offline): ALL PASSED ✅")
