"""Automatic USDC (ERC-20) allowance approval — wallet-type aware.

Sets the collateral allowance the CLOB requires before buy orders are accepted.
**Which wallet may approve depends on the signature type**, and getting this wrong
silently sets the wrong owner's allowance:

* ``signature_type=0`` (**EOA**): the signer *is* the token owner, so a direct
  ``USDC.approve(spender, amount)`` from the signer is correct. This manager can
  submit it (opt-in) via an RPC and wait for confirmation.
* ``signature_type in {1,2,3}`` (**POLY_PROXY / POLY_GNOSIS_SAFE / POLY_1271**):
  the collateral and its allowance belong to the **proxy/smart-contract wallet**,
  not the signer EOA. A raw ``approve`` from the signer would set a *different*
  owner's allowance and the CLOB would still read 0. These approvals must be
  executed *through the proxy* — done gaslessly via Polymarket's "Enable trading"
  relayer/UI. This manager refuses to broadcast a doomed/incorrect transaction in
  that case and raises with clear guidance.

Contract addresses (USDC collateral + exchange spenders) are read from the
official SDK config (``py_clob_client_v2.config.get_contract_config``); none are
hardcoded. The RPC endpoint comes from ``POLYGON_RPC_URL`` (operator-provided).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from py_clob_client_v2.config import get_contract_config

from polybot.exchange.clob import ClobExchange
from polybot.wallet.allowance import official_spenders

MAX_UINT256 = (2 ** 256) - 1

# Minimal ERC-20 ABI: approve + allowance (read-back) + decimals.
_ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "spender", "type": "address"},
                                   {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"},
                                  {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

_SIG_TYPE_NAMES = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE", 3: "POLY_1271"}


class ApprovalError(RuntimeError):
    """Raised when the USDC allowance cannot be (safely/correctly) approved."""


class ApprovalManager:
    """Ensures the collateral allowance is sufficient, approving only when valid."""

    def __init__(self, exchange: ClobExchange, *, settings: Any, signer: str, funder: str,
                 signature_type: int, logger: logging.Logger | None = None) -> None:
        self._exchange = exchange
        self._s = settings
        self._signer = signer or ""
        self._funder = funder or ""
        self._sig_type = signature_type
        self._chain_id = settings.chain_id
        self._min = settings.min_allowance
        self._log = logger or logging.getLogger("polybot.wallet.approval")

    async def ensure_allowance(self) -> dict[str, Any]:
        """Read the allowance; if short, approve (EOA only) or raise with guidance.

        Idempotent: does nothing (and sends no transaction) when the allowance
        already covers ``min_allowance`` for every spender.
        """
        # Refresh the backend's on-chain view, then read per-spender allowance.
        try:
            await self._exchange.refresh_collateral_allowance()
        except Exception as exc:  # noqa: BLE001 - advisory refresh
            self._log.debug("allowance cache refresh failed (continuing)", extra={"error": repr(exc)})
        allowances = await self._exchange.get_collateral_allowances()

        short = {sp: amt for sp, amt in (allowances or {}).items() if amt < self._min}
        if allowances and not short:
            self._log.info("USDC allowance already sufficient — no approval needed",
                           extra={"required_usdc": self._min, "allowances": allowances})
            return {"approved": False, "reason": "already_sufficient", "allowances": allowances}

        sig_name = _SIG_TYPE_NAMES.get(self._sig_type, str(self._sig_type))

        # Only a bare EOA can approve its own allowance directly.
        if self._sig_type != 0:
            raise ApprovalError(
                f"cannot auto-approve for signature_type={self._sig_type} ({sig_name}): the "
                f"collateral & allowance belong to the proxy/deposit wallet ({self._funder or '<funder>'}), "
                "not the signer EOA. A raw approve from the signer would set the wrong owner's "
                "allowance. Grant it gaslessly via the Polymarket UI ('Enable trading') for the "
                "proxy, then restart. No transaction was sent."
            )

        if not getattr(self._s, "auto_approve_allowance", False):
            raise ApprovalError(
                "USDC allowance is insufficient and AUTO_APPROVE_ALLOWANCE is disabled. "
                "Enable it (EOA wallets only) or approve manually. No transaction was sent."
            )
        rpc = getattr(self._s, "polygon_rpc_url", None)
        if not rpc:
            raise ApprovalError(
                "auto-approve requires POLYGON_RPC_URL to broadcast the approve transaction. "
                "No transaction was sent."
            )

        # EOA path: build + send approve(spender, MAX) for each short spender.
        spenders = list(short.keys()) if short else list(official_spenders(self._chain_id).keys())
        return await asyncio.to_thread(self._approve_eoa, rpc, spenders)

    def _approve_eoa(self, rpc_url: str, spenders: list[str]) -> dict[str, Any]:
        try:
            from web3 import Web3
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover
            raise ApprovalError(f"web3/eth-account required for auto-approve: {exc!r}") from exc

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if not w3.is_connected():
            raise ApprovalError(f"could not connect to POLYGON_RPC_URL for approve: {rpc_url}")

        acct = Account.from_key(self._s.private_key)
        owner = Web3.to_checksum_address(acct.address)

        gas_wei = w3.eth.get_balance(owner)
        if gas_wei == 0:
            raise ApprovalError(
                f"signer {owner} has 0 POL for gas — cannot broadcast the approve transaction. "
                "Fund the wallet with POL (gas) or use the gasless Polymarket flow."
            )

        cc = get_contract_config(self._chain_id)
        usdc = w3.eth.contract(address=Web3.to_checksum_address(cc.collateral), abi=_ERC20_ABI)

        results: list[dict[str, Any]] = []
        for spender in spenders:
            spender_cs = Web3.to_checksum_address(spender)
            tx = usdc.functions.approve(spender_cs, MAX_UINT256).build_transaction({
                "from": owner,
                "nonce": w3.eth.get_transaction_count(owner),
                "chainId": self._chain_id,
            })
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            self._log.info("USDC approve submitted", extra={"spender": spender_cs, "tx": tx_hash.hex()})
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            status = int(getattr(receipt, "status", 0))
            # Confirm the new on-chain allowance.
            new_allow_raw = usdc.functions.allowance(owner, spender_cs).call()
            new_allow = new_allow_raw / 1_000_000.0
            results.append({"spender": spender_cs, "tx": tx_hash.hex(),
                            "status": status, "allowance_usdc": new_allow})
            if status != 1:
                raise ApprovalError(f"approve tx failed on-chain (status=0) for spender {spender_cs}, "
                                    f"tx={tx_hash.hex()}")
            if new_allow < self._min:
                raise ApprovalError(f"post-approve allowance {new_allow} < required {self._min} "
                                    f"for spender {spender_cs}")
            self._log.info("USDC approve confirmed", extra={"spender": spender_cs,
                           "tx": tx_hash.hex(), "allowance_usdc": new_allow})

        return {"approved": True, "owner": owner, "results": results}
