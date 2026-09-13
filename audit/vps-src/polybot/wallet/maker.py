"""Maker / deposit-wallet authorization checks.

CLOB V2 signs every order with the **maker set to the funder** (the deposit /
proxy wallet) and the signature produced by the signer key — see
``py_clob_client_v2.order_builder.builder`` (``maker=self.funder``). The exchange
rejects orders whose maker is not an *enabled deposit wallet* for the signing
account with::

    HTTP 400  {"error":"maker address not allowed, please use the deposit wallet flow"}

This module validates — **before any order is placed** — that the maker/deposit
wallet the bot will sign for is coherent and belongs to the same account as the
L2 API credentials. It performs only read-only, server-authoritative checks and
never submits an order:

* the L2 creds (api_key/secret/passphrase) belong to the *same account* as the
  signer key — proven by ``get_api_keys`` returning the active key;
* a funder/maker is configured and, for smart-contract wallet signature types
  (``POLY_PROXY``/``POLY_GNOSIS_SAFE``/``POLY_1271``), is distinct from the bare
  signer EOA (otherwise the maker would silently default to the EOA);
* the account is not in closed-only / ban mode.

Final maker allow-listing is enforced by the exchange at submission time; a
wallet that passes these checks but has not completed Polymarket's deposit /
"Enable trading" flow will still be rejected there. In live mode a hard failure
raises :class:`MakerWalletError` so startup aborts before any order is signed.
"""
from __future__ import annotations

import logging

from polybot.exchange.clob import ClobExchange

# SignatureTypeV2: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271.
_SMART_WALLET_SIG_TYPES = {1, 2, 3}
_SIG_TYPE_NAMES = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE", 3: "POLY_1271"}


class MakerWalletError(RuntimeError):
    """Raised when the maker/deposit wallet cannot be authorized for live trading."""


class MakerWalletValidator:
    """Validates the maker/deposit wallet + credential ownership before trading."""

    def __init__(self, exchange: ClobExchange, *, signer: str, funder: str,
                 signature_type: int, api_key: str, enable_live_trading: bool,
                 logger: logging.Logger | None = None) -> None:
        self._exchange = exchange
        self._signer = (signer or "").lower()
        self._funder = (funder or "").lower()
        self._signature_type = signature_type
        self._api_key = api_key or ""
        self._live = enable_live_trading
        self._log = logger or logging.getLogger("polybot.wallet.maker")

    async def validate(self) -> dict[str, object]:
        """Run all checks. Returns a summary dict; raises in live mode on failure.

        The **maker address used for every order is the funder** (or the signer
        itself for a bare EOA). It is logged explicitly so the operator can
        confirm which wallet the bot signs for.
        """
        sig_name = _SIG_TYPE_NAMES.get(self._signature_type, str(self._signature_type))
        maker = self._funder or self._signer  # mirrors OrderBuilder: maker = funder or signer

        problems: list[str] = []

        # 1) A signer must exist (key loaded).
        if not self._signer:
            problems.append("no signer address (private key not loaded)")

        # 2) Maker/funder must be coherent with the signature type.
        if self._signature_type in _SMART_WALLET_SIG_TYPES:
            if not self._funder:
                problems.append(
                    f"signature_type={self._signature_type} ({sig_name}) requires a funder/"
                    "deposit-wallet address (POLYMARKET_PROXY_ADDRESS), but none is set"
                )
            elif self._funder == self._signer:
                problems.append(
                    f"signature_type={self._signature_type} ({sig_name}) expects the maker/"
                    "deposit wallet to differ from the signer EOA, but funder == signer "
                    f"({self._signer})"
                )
        elif self._signature_type == 0:
            if self._funder and self._funder != self._signer:
                # EOA maker is the signer itself; a different funder is contradictory.
                self._log.warning(
                    "signature_type=0 (EOA) signs as the signer EOA, but a different funder "
                    "is configured; the configured funder will be used as maker",
                    extra={"signer": self._signer, "funder": self._funder})

        # 3) L2 creds must belong to the same account as the signer key.
        creds_ok = False
        try:
            keys = await self._exchange.api_keys()
            creds_ok = bool(self._api_key) and self._api_key in keys
            if not creds_ok:
                problems.append(
                    "L2 API credentials do not belong to the signer's account "
                    "(active api_key not returned by get_api_keys); api_key/secret/passphrase "
                    "and the private key must be from the SAME Polymarket account"
                )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not verify credential ownership (get_api_keys failed): {exc!r}")

        # 4) Account must not be in closed-only / ban mode.
        closed_only = None
        try:
            closed_only = await self._exchange.closed_only_mode()
            if closed_only:
                problems.append("account is in CLOSED-ONLY (ban) mode — new orders are not allowed")
        except Exception as exc:  # noqa: BLE001
            self._log.warning("could not read ban/closed-only status (continuing)",
                              extra={"error": repr(exc)})

        summary = {
            "maker_address": maker,
            "signer": self._signer,
            "funder": self._funder,
            "signature_type": self._signature_type,
            "signature_type_name": sig_name,
            "creds_belong_to_account": creds_ok,
            "closed_only": closed_only,
            "authorized": not problems,
        }
        self._log.info("maker/deposit wallet — orders will be signed with this maker",
                       extra=summary)

        if problems:
            return self._fail("; ".join(problems), summary)

        self._log.info(
            "maker/deposit wallet validated: creds belong to the signer's account and the "
            "maker is coherent with the signature type. NOTE: final maker allow-listing is "
            "enforced by the CLOB at order submission — the deposit wallet must have completed "
            "Polymarket's deposit/'Enable trading' flow or orders are rejected with "
            "'maker address not allowed'.",
            extra={"maker_address": maker})
        return summary

    def _fail(self, message: str, summary: dict[str, object]) -> dict[str, object]:
        """Abort in live mode (raise); warn and continue in dry-run."""
        if self._live:
            self._log.critical("maker/deposit-wallet validation failed — blocking live trading",
                               extra={"reason": message, **summary})
            raise MakerWalletError(message)
        self._log.warning("maker/deposit-wallet validation not satisfied (allowed in dry-run; "
                          "live trading would be blocked until resolved)",
                          extra={"reason": message, **summary})
        return summary
