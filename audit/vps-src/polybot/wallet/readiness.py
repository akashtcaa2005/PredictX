"""Consolidated live-trading readiness gate.

Combines the individual startup checks — authentication, maker/deposit-wallet
authorization, USDC allowance, USDC balance — into a single server-authoritative
verdict with explicit, actionable messaging, and publishes it to
``state/readiness.json`` for monitoring.

It is **read-only** and does **not** modify strategy, risk, indicators, or
order-execution logic. Enforcement (blocking live trading) is done by the
existing startup gates (maker/allowance/balance), which raise before any order
is created; this reporter names exactly which requirement is missing.

Important: the Polymarket **"Enable Trading" / deposit-wallet flow is a gasless
relayer action performed in the Polymarket web UI** — it is not exposed by the
CLOB v2 SDK/API and cannot be invoked programmatically, and a raw ERC-20 approve
from the signer is invalid for a proxy wallet (wrong owner). The on-chain effect
of completing it is a non-zero USDC allowance for the exchange spenders, which is
exactly what this gate detects.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from polybot.exchange.clob import ClobExchange
from polybot.wallet.allowance import official_spenders

_SIG_TYPE_NAMES = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE", 3: "POLY_1271"}
_SMART_WALLET_SIG_TYPES = {1, 2, 3}


class TradingReadiness:
    """Aggregates the pre-trade requirements into one verdict + artifact."""

    def __init__(self, exchange: ClobExchange, *, signer: str, funder: str, signature_type: int,
                 api_key: str, chain_id: int, min_allowance: float, min_balance: float,
                 logger: logging.Logger | None = None, readiness_file: Any = None) -> None:
        self._ex = exchange
        self._signer = (signer or "").lower()
        self._funder = (funder or "").lower()
        self._sig_type = signature_type
        self._api_key = api_key or ""
        self._chain_id = chain_id
        self._min_allowance = min_allowance
        self._min_balance = min_balance
        self._log = logger or logging.getLogger("polybot.wallet.readiness")
        self._file = Path(readiness_file) if readiness_file else None

    async def evaluate(self) -> dict[str, Any]:
        """Compute the consolidated readiness report (never raises)."""
        sig_name = _SIG_TYPE_NAMES.get(self._sig_type, str(self._sig_type))
        is_proxy = self._sig_type in _SMART_WALLET_SIG_TYPES
        maker = self._funder or self._signer  # OrderBuilder uses maker = funder or signer

        checks: dict[str, dict[str, Any]] = {}
        missing: list[str] = []

        # 1) Authentication — L2 creds belong to the signer's account.
        try:
            keys = await self._ex.api_keys()
            auth_ok = bool(self._api_key) and self._api_key in keys
        except Exception as exc:  # noqa: BLE001
            auth_ok = False
            self._log.warning("readiness: get_api_keys failed", extra={"error": repr(exc)})
        checks["authentication"] = {"ok": auth_ok,
            "detail": "L2 api_key belongs to signer account" if auth_ok
                      else "api_key/secret/passphrase not linked to the signing key's account"}
        if not auth_ok:
            missing.append("AUTHENTICATION: API key/secret/passphrase are not linked to the signer account")

        # 2) Ban / closed-only status.
        try:
            closed_only = await self._ex.closed_only_mode()
        except Exception:  # noqa: BLE001
            closed_only = None
        checks["not_closed_only"] = {"ok": (closed_only is False),
            "detail": f"closed_only={closed_only}"}
        if closed_only:
            missing.append("ACCOUNT: in closed-only/ban mode — new orders are not allowed")

        # 3) Maker / deposit-wallet coherence (linkage to the account).
        maker_ok = True
        if is_proxy and (not self._funder or self._funder == self._signer):
            maker_ok = False
            missing.append(
                f"MAKER/DEPOSIT WALLET: signature_type={self._sig_type} ({sig_name}) needs a "
                "deposit/proxy wallet distinct from the signer (POLYMARKET_PROXY_ADDRESS)")
        checks["maker_wallet"] = {"ok": maker_ok, "detail": f"maker={maker} sig_type={sig_name}"}

        # 4) USDC allowance for every required spender (official SDK config).
        spenders = official_spenders(self._chain_id)  # {addr_lower: label}
        try:
            raw = await self._ex.get_collateral_allowances()
        except Exception as exc:  # noqa: BLE001
            raw = {}
            self._log.warning("readiness: allowance read failed", extra={"error": repr(exc)})
        raw_lower = {str(k).lower(): v for k, v in (raw or {}).items()}
        allowance_by_spender = {label: float(raw_lower.get(addr, 0.0)) for addr, label in spenders.items()}
        allowance_positive = any(v > 0 for v in allowance_by_spender.values())
        allowance_sufficient = bool(allowance_by_spender) and all(
            v >= self._min_allowance for v in allowance_by_spender.values())
        checks["usdc_allowance"] = {"ok": allowance_sufficient,
            "required_usdc": self._min_allowance, "per_spender": allowance_by_spender}

        # "Deposit wallet enabled" == the on-chain effect of Enable Trading, i.e. a
        # non-zero allowance for the exchange spenders.
        deposit_enabled = allowance_positive
        checks["deposit_wallet_enabled"] = {"ok": deposit_enabled,
            "detail": "allowance>0 for exchange spenders" if deposit_enabled
                      else "no allowance set — Enable Trading not completed"}
        if not deposit_enabled:
            missing.append(
                "DEPOSIT WALLET: not enabled — complete Polymarket's gasless 'Enable Trading' "
                f"flow in the UI for {maker} (do NOT use a raw ERC-20 approve for a proxy wallet). "
                "This sets the USDC allowance for the exchange spenders.")
        elif not allowance_sufficient:
            missing.append(
                f"USDC ALLOWANCE: below required {self._min_allowance} for one or more spenders "
                f"({allowance_by_spender})")

        # 5) USDC balance.
        try:
            balance = await self._ex.get_collateral_balance()
        except Exception as exc:  # noqa: BLE001
            balance = 0.0
            self._log.warning("readiness: balance read failed", extra={"error": repr(exc)})
        balance_ok = balance >= self._min_balance
        checks["usdc_balance"] = {"ok": balance_ok, "balance": balance, "required": self._min_balance}
        if not balance_ok:
            missing.append(f"USDC BALANCE: {balance} < required minimum {self._min_balance} — deposit USDC")

        live_ready = not missing
        report = {
            "live_ready": live_ready,
            "signer": self._signer,
            "maker_deposit_wallet": maker,
            "signature_type": self._sig_type,
            "signature_type_name": sig_name,
            "checks": checks,
            "missing_requirements": missing,
        }

        if live_ready:
            self._log.info("TRADING READINESS: all pre-trade requirements satisfied", extra=report)
        else:
            self._log.warning("TRADING READINESS: NOT ready for live trading — missing requirements",
                              extra={"missing": missing, "maker_deposit_wallet": maker})
            for item in missing:
                self._log.warning("readiness blocker", extra={"requirement": item})
        self._write(report)
        return report

    def _write(self, report: dict[str, Any]) -> None:
        if self._file is None:
            return
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(report, default=str, indent=2))
            os.replace(tmp, self._file)
        except Exception as exc:  # noqa: BLE001 - reporting is best-effort
            self._log.debug("readiness.json write failed", extra={"error": repr(exc)})
