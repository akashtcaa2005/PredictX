"""Paper trading engine: account ledger, matching, and wallet isolation.

No network and no wallet: the simulated exchange is driven with synthetic books.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from polybot.config.settings import WALLET_SECRET_ENV, Settings, scrub_wallet_secrets
from polybot.orders.models import Action, OrderIntent
from polybot.paper.account import PaperAccount, PaperBalanceManager
from polybot.paper.exchange import PaperExchange


def intent(action=Action.BUY, price=0.43, size=40.0, market="M1", side="up", token="T1"):
    return OrderIntent(market_id=market, strategy_side=side, token_id=token,
                       action=action, price=price, size=size)


@pytest.fixture()
def exchange():
    account = PaperAccount(100.0)
    ex = PaperExchange(reader=None, account=account, fee_bps=0, fill_latency_ms=0)
    ex.apply_book("T1", bids=[{"price": 0.40, "size": 50}],
                  asks=[{"price": 0.42, "size": 30}, {"price": 0.45, "size": 100}])
    return ex, account


# ======================================================================
# Account ledger
# ======================================================================
def test_reserve_blocks_overcommitment():
    account = PaperAccount(10.0)
    assert account.reserve(6.0) is True
    assert account.reserve(6.0) is False, "the same dollar must not be committed twice"
    assert account.available == 4.0
    account.release(6.0)
    assert account.available == 10.0


def test_ledger_persists_and_restores(tmp_path):
    path = tmp_path / "account.json"
    account = PaperAccount(500.0, state_file=path)
    account.debit(120.0, fee=0.5)
    assert account.cash == pytest.approx(379.5)

    restored = PaperAccount(500.0, state_file=path)
    assert restored.cash == pytest.approx(379.5)
    assert restored.fees_paid == pytest.approx(0.5)
    assert restored.locked == 0.0, "reservations are rebuilt from live orders, not restored"


def test_balance_manager_uses_available_not_total():
    account = PaperAccount(100.0)
    account.reserve(90.0)
    balance = PaperBalanceManager(account, min_balance=0.0)
    assert balance.balance == 100.0
    assert balance.has_funds_for(5.0) is True
    assert balance.has_funds_for(20.0) is False


# ======================================================================
# Matching
# ======================================================================
def test_marketable_buy_fills_at_the_book_not_the_limit(exchange):
    ex, account = exchange
    result = asyncio.run(ex.place_order(intent(), tick_size=0.01))
    assert result["success"] is True

    fills = ex.match()
    assert len(fills) == 1
    assert fills[0]["price"] == pytest.approx(0.42), "should fill at the ask, not the 0.43 limit"
    assert fills[0]["size"] == pytest.approx(30.0), "limited by the liquidity at that level"
    assert account.cash == pytest.approx(100.0 - 30 * 0.42)


def test_unmarketable_order_rests(exchange):
    ex, _ = exchange
    asyncio.run(ex.place_order(intent(price=0.30), tick_size=0.01))
    assert ex.match() == [], "a bid below the best ask must not fill"
    assert len(asyncio.run(ex.get_orders())) == 1


def test_insufficient_collateral_is_rejected():
    account = PaperAccount(5.0)
    ex = PaperExchange(reader=None, account=account, fill_latency_ms=0)
    result = asyncio.run(ex.place_order(intent(price=0.50, size=100.0)))
    assert result["success"] is False
    assert "insufficient paper collateral" in result["errorMsg"]


def test_sell_without_inventory_is_rejected():
    account = PaperAccount(100.0)
    ex = PaperExchange(reader=None, account=account, fill_latency_ms=0,
                       inventory_provider=lambda market, side: 3.0)
    result = asyncio.run(ex.place_order(intent(action=Action.SELL, price=0.10, size=10.0)))
    assert result["success"] is False
    assert "insufficient paper shares" in result["errorMsg"]


def test_fill_latency_defers_execution():
    account = PaperAccount(100.0)
    ex = PaperExchange(reader=None, account=account, fill_latency_ms=60_000)
    ex.apply_book("T1", bids=[], asks=[{"price": 0.10, "size": 100}])
    asyncio.run(ex.place_order(intent(price=0.20, size=10.0)))
    assert ex.match() == [], "an order younger than the latency window must not fill"


def test_partial_fill_ratio_produces_partials():
    account = PaperAccount(100.0)
    ex = PaperExchange(reader=None, account=account, fill_latency_ms=0, maker_fill_ratio=0.5)
    ex.apply_book("T1", bids=[], asks=[{"price": 0.10, "size": 1000}])
    asyncio.run(ex.place_order(intent(price=0.20, size=100.0)))

    first = ex.match()[0]
    assert first["size"] == pytest.approx(50.0)
    rows = asyncio.run(ex.get_orders())
    assert rows[0]["status"] == "partially_filled"
    assert rows[0]["size_matched"] == pytest.approx(50.0)


def test_slippage_worsens_the_execution_price():
    account = PaperAccount(100.0)
    ex = PaperExchange(reader=None, account=account, fill_latency_ms=0, slippage_ticks=2)
    ex.apply_book("T1", bids=[], asks=[{"price": 0.30, "size": 100}])
    asyncio.run(ex.place_order(intent(price=0.40, size=10.0), tick_size=0.01))
    fill = ex.match()[0]
    assert fill["price"] == pytest.approx(0.32), "2 ticks of slippage against the buyer"


def test_fees_are_charged_on_notional():
    account = PaperAccount(100.0)
    ex = PaperExchange(reader=None, account=account, fee_bps=100, fill_latency_ms=0)  # 1%
    ex.apply_book("T1", bids=[], asks=[{"price": 0.50, "size": 100}])
    asyncio.run(ex.place_order(intent(price=0.50, size=10.0)))
    fill = ex.match()[0]
    assert fill["fee"] == pytest.approx(0.05)
    assert account.fees_paid == pytest.approx(0.05)


def test_cancel_returns_reserved_collateral(exchange):
    ex, account = exchange
    result = asyncio.run(ex.place_order(intent(price=0.30, size=100.0)))
    assert account.locked == pytest.approx(30.0)
    asyncio.run(ex.cancel(result["orderID"]))
    assert account.locked == 0.0
    assert account.cash == pytest.approx(100.0)


def test_cancel_market_orders_targets_only_that_market():
    account = PaperAccount(1000.0)
    ex = PaperExchange(reader=None, account=account, fill_latency_ms=0)
    asyncio.run(ex.place_order(intent(market="M1", price=0.10, size=10)))
    asyncio.run(ex.place_order(intent(market="M2", price=0.10, size=10)))
    asyncio.run(ex.cancel_market_orders(market="M1"))
    open_orders = asyncio.run(ex.get_orders())
    assert [o["market"] for o in open_orders] == ["M2"]


def test_liquidity_is_consumed_within_a_matching_pass():
    """Two orders competing for the same level must not both take it whole."""
    account = PaperAccount(1000.0)
    ex = PaperExchange(reader=None, account=account, fill_latency_ms=0)
    ex.apply_book("T1", bids=[], asks=[{"price": 0.10, "size": 30}])
    asyncio.run(ex.place_order(intent(price=0.20, size=30.0)))
    asyncio.run(ex.place_order(intent(price=0.20, size=30.0)))
    fills = ex.match()
    assert sum(f["size"] for f in fills) == pytest.approx(30.0)


def test_order_rows_match_the_clob_shape(exchange):
    """The reconciler reads these rows; the field names must line up."""
    ex, _ = exchange
    asyncio.run(ex.place_order(intent(price=0.30)))
    row = asyncio.run(ex.get_orders())[0]
    for field in ("id", "asset_id", "side", "price", "original_size", "size_matched", "status"):
        assert field in row


# ======================================================================
# Wallet isolation
# ======================================================================
def test_scrub_removes_every_wallet_variable(monkeypatch):
    for name in WALLET_SECRET_ENV:
        monkeypatch.setenv(name, "sensitive")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

    removed = scrub_wallet_secrets()

    assert set(removed) == set(WALLET_SECRET_ENV)
    for name in WALLET_SECRET_ENV:
        assert name not in os.environ
    assert os.environ["ENABLE_LIVE_TRADING"] == "false"


def test_paper_settings_never_carry_a_key(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("POLYMARKET_PROXY_ADDRESS", "0xdeadbeef")
    monkeypatch.setenv("MARKETS", "0xabc")
    # Explicit LOG_DIR/STATE_DIR intentionally override the mode defaults, so
    # clear anything another module left behind before asserting on them.
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.delenv("ENGINE_DB", raising=False)
    env_file = tmp_path / "env.paper.txt"
    env_file.write_text("MARKETS=0xabc\nPAPER_STARTING_CASH=250\n")

    settings = Settings.load(env_file, mode="paper")

    assert settings.private_key == ""
    assert settings.proxy_address == ""
    assert settings.enable_live_trading is False
    assert settings.paper_starting_cash == 250.0
    assert "paper" in str(settings.db_path)
    assert "paper" in str(settings.log_dir)


def test_paper_and_live_paths_never_overlap():
    from polybot.config.paths import mode_paths

    paper, live = mode_paths("paper"), mode_paths("live")
    assert paper.db != live.db
    assert paper.logs != live.logs
    assert paper.state != live.state
    assert paper.kill_switch != live.kill_switch
