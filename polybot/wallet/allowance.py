"""Collateral (USDC) allowance verification.

Before the bot can place **buy** orders, the collateral wallet must have granted
a USDC allowance to Polymarket's exchange contracts. If the allowance is 0 the
CLOB rejects every order.

How allowances get set depends on the wallet model:

* **Polymarket proxy wallet** (``signature_type`` 1 / 2, the default here): the
  proxy contract holds the funds and must approve the exchanges. This is done
  **gaslessly via the Polymarket UI/relayer** ("Enable trading"), not by this
  bot — a plain ERC-20 approve from the signer EOA would set the wrong address's
  allowance. This manager therefore **verifies** the allowance and refuses to
  arm live trading until it is set; it never submits an on-chain transaction.
* **Plain EOA** (``signature_type`` 0): the EOA itself would approve on-chain.

The manager is safe to run in dry-run (it only reads and logs) and is
idempotent: it never re-approves when the allowance already covers the required
amount (requirement: "never repeatedly if already approved").
"""
from __future__ import annotations

import logging

from polybot.exchange.clob import ClobExchange

POLYGON_CHAIN_ID = 137

# Human-readable names for the known Polymarket collateral spenders, purely for
# clearer logs. Unknown spenders are logged by address.
_SPENDER_NAMES = {
    "0xE111180000d2663C0091e4f400237545B87B996B": "CTF Exchange",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": "NegRisk Exchange",
    "0xe2222d279d744050d28e00520010520000310F59": "NegRisk Adapter",
}


class AllowanceError(RuntimeError):
    """Raised when collateral allowance is insufficient to trade live."""


def _spender_label(address: str) -> str:
    return _SPENDER_NAMES.get(address, _SPENDER_NAMES.get(address.lower(), address))


class AllowanceManager:
    """Verifies the collateral allowance before live trading is armed."""

    def __init__(self, exchange: ClobExchange, *, signature_type: int, chain_id: int,
                 min_allowance: float, enable_live_trading: bool,
                 funder: str = "", logger: logging.Logger | None = None) -> None:
        self._exchange = exchange
        self._sig_type = signature_type
        self._chain_id = chain_id
        self._min = min_allowance
        self._live = enable_live_trading
        self._funder = funder
        self._log = logger or logging.getLogger("polybot.wallet")

    async def verify(self) -> bool:
        """Check the collateral allowance for every exchange spender.

        Returns ``True`` when the allowance covers ``min_allowance`` for all
        spenders. In live mode a shortfall (or a misconfigured network) raises
        :class:`AllowanceError` so startup aborts *before* any buy order is
        placed. In dry-run it logs and returns ``False`` without raising.
        """
        # (7) Correct-network guard: only meaningful on Polygon mainnet.
        if self._chain_id != POLYGON_CHAIN_ID:
            msg = (f"allowance check: CHAIN_ID={self._chain_id} is not Polygon mainnet "
                   f"({POLYGON_CHAIN_ID}); refusing to assume allowances are valid")
            return self._fail(msg)

        # Ask the backend to re-read on-chain state first, so an allowance just
        # granted in the Polymarket UI is reflected immediately. Best-effort.
        try:
            await self._exchange.refresh_collateral_allowance()
        except Exception as exc:  # noqa: BLE001 - refresh is advisory only
            self._log.debug("allowance cache refresh failed (continuing)",
                            extra={"error": repr(exc)})

        try:
            allowances = await self._exchange.get_collateral_allowances()
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"could not read collateral allowance: {exc!r}")

        if not allowances:
            return self._fail("collateral allowance unavailable from CLOB (treated as 0)")

        # (6) Clear logging of the current allowance for every spender.
        shortfalls: list[str] = []
        for spender, amount in allowances.items():
            label = _spender_label(spender)
            ok = amount >= self._min
            self._log.info("collateral allowance", extra={
                "spender": label, "address": spender,
                "allowance_usdc": round(amount, 6), "required_usdc": self._min,
                "sufficient": ok})
            if not ok:
                shortfalls.append(f"{label} ({amount:.6f} < {self._min:.6f})")

        if not shortfalls:
            # (5) Already approved -> skip; (7) idempotent, nothing is submitted.
            self._log.info("collateral allowance OK — sufficient for all exchanges; "
                           "no approval needed", extra={"required_usdc": self._min})
            return True

        return self._fail(
            "insufficient USDC allowance for: " + "; ".join(shortfalls) + ". "
            "This wallet is a Polymarket proxy — approvals must be granted gaslessly "
            f"via the Polymarket UI ('Enable trading') for proxy {self._funder or '<funder>'}, "
            "then restart the bot. No on-chain approve is sent by the bot."
        )

    def _fail(self, message: str) -> bool:
        """Abort in live mode (raise); warn and continue in dry-run."""
        if self._live:
            self._log.critical("allowance check failed — blocking live trading",
                               extra={"reason": message})
            raise AllowanceError(message)
        self._log.warning("allowance check: not satisfied (allowed in dry-run; live "
                          "trading would be blocked until resolved)",
                          extra={"reason": message})
        return False
