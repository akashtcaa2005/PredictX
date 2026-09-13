"""Async wrapper over the (synchronous) ``py-clob-client-v2``.

All blocking SDK calls are dispatched to a worker thread via
:func:`asyncio.to_thread` so the event loop never blocks, and are wrapped with
retry/backoff + latency metrics. Read/cancel calls are retried; order *posting*
is deliberately not retried, to avoid duplicate live orders when a response is
lost after the order actually landed.

This targets the **CLOB V2** SDK. Notable API differences from the archived
v1 client that this facade absorbs:

* open-order listing is ``get_open_orders`` (was ``get_orders``);
* single-order cancel takes an ``OrderPayload`` (was a bare id);
* market-scoped cancel takes an ``OrderMarketCancelParams`` (was two args).

Order create/sign/post remain a deliberate two step
(``create_order`` -> ``post_order``) so posting is never retried.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from polybot.monitoring.metrics import Metrics
from polybot.orders.models import Action, OrderIntent
from polybot.utils.retry import RetryPolicy, retry_async
from polybot.utils.time_utils import now_ms


class ClobExchange:
    """Typed, async facade over the CLOB client used by the rest of the engine."""

    def __init__(self, client: Any, *, signature_type: int, retry_policy: RetryPolicy,
                 logger: logging.Logger | None = None, metrics: Metrics | None = None) -> None:
        self._c = client
        self._signature_type = signature_type
        self._policy = retry_policy
        self._no_retry = RetryPolicy(attempts=1)
        self._log = logger or logging.getLogger("polybot.exchange.clob")
        self._metrics = metrics
        # The underlying py_clob_client_v2 client wraps a single, non-thread-safe
        # requests.Session. Concurrent to_thread dispatches (e.g. the dashboard
        # firing several account syncs at once) would then share one socket and
        # raise "[Errno 11] Resource temporarily unavailable". This lock makes the
        # shared client strictly one-call-at-a-time; the engine already calls
        # sequentially, so it adds no contention there.
        self._call_lock = asyncio.Lock()

        from py_clob_client_v2 import AssetType, OrderType
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        self._AssetType = AssetType
        self._order_types = {"GTC": OrderType.GTC, "GTD": OrderType.GTD,
                             "FOK": OrderType.FOK, "FAK": OrderType.FAK}
        self._sides = {Action.BUY: BUY, Action.SELL: SELL}

    # -- internal dispatch -----------------------------------------------------
    async def _call(self, fn: Any, *args: Any, desc: str, policy: RetryPolicy | None = None) -> Any:
        async def _once() -> Any:
            start = now_ms()
            try:
                async with self._call_lock:
                    return await asyncio.to_thread(fn, *args)
            finally:
                if self._metrics:
                    self._metrics.observe_latency("clob.latency", now_ms() - start)

        return await retry_async(_once, policy=policy or self._policy, description=desc)

    # -- health / identity -----------------------------------------------------
    async def health(self) -> bool:
        try:
            await self._call(self._c.get_ok, desc="get_ok")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def server_time(self) -> Any:
        return await self._call(self._c.get_server_time, desc="get_server_time")

    def address(self) -> str:
        return self._c.get_address()

    # -- market metadata / data ------------------------------------------------
    async def get_market(self, condition_id: str) -> dict[str, Any]:
        return await self._call(self._c.get_market, condition_id, desc=f"get_market {condition_id}")

    async def get_order_book(self, token_id: str) -> Any:
        return await self._call(self._c.get_order_book, token_id, desc=f"get_order_book {token_id}")

    async def get_tick_size(self, token_id: str) -> float:
        return float(await self._call(self._c.get_tick_size, token_id, desc="get_tick_size"))

    async def get_neg_risk(self, token_id: str) -> bool:
        return bool(await self._call(self._c.get_neg_risk, token_id, desc="get_neg_risk"))

    async def best_bid_ask(self, token_id: str) -> dict[str, float]:
        """Best bid/ask (and book metadata) for a token; 0.0 where no quote."""
        book = await self.get_order_book(token_id)
        bids = getattr(book, "bids", None) or []
        asks = getattr(book, "asks", None) or []
        best_bid = max((float(b.price) for b in bids), default=0.0)
        best_ask = min((float(a.price) for a in asks), default=0.0)
        return {
            "bid": best_bid,
            "ask": best_ask,
            "tick_size": float(getattr(book, "tick_size", 0.01) or 0.01),
            "min_order_size": float(getattr(book, "min_order_size", 0.0) or 0.0),
            "neg_risk": bool(getattr(book, "neg_risk", False)),
        }

    # -- orders ----------------------------------------------------------------
    async def place_order(self, intent: OrderIntent, *, tick_size: float | None = None,
                          neg_risk: bool = False) -> dict[str, Any]:
        """Create, sign and post an order. Returns the raw CLOB response dict."""
        from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions

        args = OrderArgs(
            token_id=intent.token_id,
            price=float(intent.price),
            size=float(intent.size),
            side=self._sides[intent.action],
        )
        options = None
        if tick_size is not None:
            options = PartialCreateOrderOptions(tick_size=_fmt_tick(tick_size), neg_risk=neg_risk)

        signed = await self._call(self._c.create_order, args, options,
                                  desc="create_order", policy=self._no_retry)
        order_type = self._order_types.get(intent.order_type, self._order_types["GTC"])
        resp = await self._call(self._c.post_order, signed, order_type,
                                desc="post_order", policy=self._no_retry)
        return resp if isinstance(resp, dict) else {"raw": resp}

    async def cancel(self, order_id: str) -> Any:
        from py_clob_client_v2 import OrderPayload

        payload = OrderPayload(orderID=order_id)
        return await self._call(self._c.cancel_order, payload, desc=f"cancel {order_id}")

    async def cancel_orders(self, order_ids: list[str]) -> Any:
        return await self._call(self._c.cancel_orders, order_ids, desc="cancel_orders")

    async def cancel_all(self) -> Any:
        return await self._call(self._c.cancel_all, desc="cancel_all")

    async def cancel_market_orders(self, *, market: str = "", asset_id: str = "") -> Any:
        from py_clob_client_v2 import OrderMarketCancelParams

        params = OrderMarketCancelParams(market=market or None, asset_id=asset_id or None)
        return await self._call(self._c.cancel_market_orders, params,
                                desc=f"cancel_market_orders {market or asset_id}")

    async def get_orders(self, *, market: str | None = None, asset_id: str | None = None) -> list[dict[str, Any]]:
        from py_clob_client_v2 import OpenOrderParams

        params = OpenOrderParams(market=market or None, asset_id=asset_id or None)
        result = await self._call(self._c.get_open_orders, params, desc="get_open_orders")
        return result if isinstance(result, list) else list(result or [])

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        return await self._call(self._c.get_order, order_id, desc=f"get_order {order_id}")

    # -- account / auth introspection (read-only) ------------------------------
    async def api_keys(self) -> list[str]:
        """L2 API keys registered for the authenticated signer's account.

        Used to prove the api_key/secret/passphrase belong to the same account as
        the signing key. Returns a list of api_key strings.
        """
        result = await self._call(self._c.get_api_keys, desc="get_api_keys")
        if isinstance(result, dict):
            return [str(k) for k in (result.get("apiKeys") or [])]
        return [str(k) for k in (result or [])]

    async def closed_only_mode(self) -> bool:
        """Whether the account is in closed-only / ban mode (no new orders)."""
        result = await self._call(self._c.get_closed_only_mode, desc="get_closed_only_mode")
        if isinstance(result, dict):
            return bool(result.get("closed_only"))
        return bool(result)

    async def get_trades(self, *, market: str | None = None, asset_id: str | None = None) -> list[dict[str, Any]]:
        from py_clob_client_v2 import TradeParams

        params = TradeParams(market=market or None, asset_id=asset_id or None)
        result = await self._call(self._c.get_trades, params, desc="get_trades")
        return result if isinstance(result, list) else list(result or [])

    # -- balances --------------------------------------------------------------
    async def get_collateral_balance(self) -> float:
        """USDC (collateral) balance in human units."""
        from py_clob_client_v2 import BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=self._AssetType.COLLATERAL,
                                        signature_type=self._signature_type)
        result = await self._call(self._c.get_balance_allowance, params, desc="get_balance_allowance")
        return _parse_balance(result)

    async def get_collateral_allowances(self) -> dict[str, float]:
        """Per-spender USDC allowance (human units) granted to the exchange contracts.

        Returns a mapping of ``{spender_address: allowance_usdc}``. An empty dict
        means the backend returned no allowance data (treated as unknown/zero by
        callers).
        """
        from py_clob_client_v2 import BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=self._AssetType.COLLATERAL,
                                        signature_type=self._signature_type)
        result = await self._call(self._c.get_balance_allowance, params, desc="get_balance_allowance")
        return _parse_allowances(result)

    async def refresh_collateral_allowance(self) -> Any:
        """Ask the CLOB backend to re-read this wallet's on-chain USDC balance &
        allowance into its cache.

        IMPORTANT: this is a server-side cache refresh (``GET
        /balance-allowance/update``); it does **not** submit an on-chain approve.
        Real allowance changes must be granted by the wallet on-chain — for a
        Polymarket proxy wallet (signature_type=1) via the Polymarket UI/relayer.
        Call this after enabling trading in the UI so the backend picks up the
        new allowance immediately.
        """
        from py_clob_client_v2 import BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=self._AssetType.COLLATERAL,
                                        signature_type=self._signature_type)
        return await self._call(self._c.update_balance_allowance, params, desc="refresh_balance_allowance")


def _fmt_tick(tick: float) -> str:
    """Format a tick size the way the SDK expects.

    CLOB V2 supports the tick-size ladder
    ``0.1 / 0.01 / 0.005 / 0.0025 / 0.001 / 0.0001``.
    """
    for candidate in ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"):
        if abs(tick - float(candidate)) < 1e-9:
            return candidate
    return format(tick, "f").rstrip("0").rstrip(".") or "0.01"


def _parse_balance(result: Any) -> float:
    """Convert a balance-allowance response (base units, 6dp USDC) to human units."""
    if isinstance(result, dict):
        raw = result.get("balance", 0)
    else:
        raw = getattr(result, "balance", 0)
    try:
        return float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def _to_usdc(raw: Any) -> float:
    """Base-unit (6dp) integer/str -> human USDC units; 0.0 on bad input."""
    try:
        return float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def _parse_allowances(result: Any) -> dict[str, float]:
    """Extract per-spender allowances (human USDC units) from a balance-allowance
    response. Handles the dict-keyed-by-spender shape returned for proxy wallets
    and a single scalar allowance (plain EOA)."""
    if isinstance(result, dict):
        raw = result.get("allowances")
        if raw is None:
            raw = result.get("allowance")
    else:
        raw = getattr(result, "allowances", None)
        if raw is None:
            raw = getattr(result, "allowance", None)
    if isinstance(raw, dict):
        return {str(spender): _to_usdc(val) for spender, val in raw.items()}
    if raw is None:
        return {}
    # Scalar allowance (e.g. a plain EOA response).
    return {"collateral": _to_usdc(raw)}
