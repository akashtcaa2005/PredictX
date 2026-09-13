#!/usr/bin/env python3
"""Deployment preflight for the Polymarket bot.

Order of checks (fails fast, secrets never printed — only masked fingerprints):

1. Parse configuration from env.txt.
2. GATE: CAMBER_API_TOKEN must be present (from the environment / env.txt).
3. Load the compiled strategy read-only and confirm the .pyc is unchanged.
4. Verify network connectivity to the Polymarket API.

Exit codes: 0 ok · 3 token missing · 4 strategy load failed ·
5 Polymarket unreachable · 6 config error.
"""
from __future__ import annotations

import hashlib
import socket
import ssl
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _fp(secret: str) -> str:
    """Non-reversible short fingerprint — safe proof-of-value, not the secret."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8] + "…"


def _mask_addr(addr: str) -> str:
    if not addr:
        return "(unset)"
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else "****"


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]


def _check_connectivity(hosts: list[str], timeout: float = 4.0) -> bool:
    reachable = False
    for h in hosts:
        start = time.time()
        try:
            sock = socket.create_connection((h, 443), timeout=timeout)
            ssl.create_default_context().wrap_socket(sock, server_hostname=h).close()
            print(f"  [ok]   {h}:443 reachable ({time.time() - start:.1f}s)")
            reachable = True
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {h}:443 unreachable — {type(exc).__name__} ({time.time() - start:.1f}s)")
    return reachable


def main() -> int:
    from polybot.config.settings import ConfigError, Settings

    print("== [1/5] configuration (env.txt) ==")
    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"  [FAIL] configuration invalid: {exc}")
        return 6
    print(f"  [ok]   env.txt parsed — mode={'LIVE' if settings.enable_live_trading else 'DRY-RUN'}")
    print(f"         funder/proxy: {_mask_addr(settings.proxy_address)}  signature_type={settings.signature_type}")
    print(f"         markets configured: {len(settings.markets)}")

    print("== [2/5] CAMBER_API_TOKEN gate ==")
    token = settings.camber_api_token
    if not token:
        print("  [FAIL] CAMBER_API_TOKEN is not set (environment or env.txt) — refusing to continue")
        return 3
    print(f"  [ok]   CAMBER_API_TOKEN present (len={len(token)}, fp={_fp(token)}) — value never logged")

    print("== [3/5] compiled strategy (loaded read-only, not modified) ==")
    pyc = REPO / "Strategy" / "s1_cheap_buy.cpython-313.pyc"
    before = hashlib.sha256(pyc.read_bytes()).hexdigest()
    try:
        from polybot.strategy.loader import load_strategy_class

        cls = load_strategy_class()
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] could not load strategy: {exc!r}")
        return 4
    after = hashlib.sha256(pyc.read_bytes()).hexdigest()
    if before != after:
        print("  [FAIL] strategy .pyc changed during load")
        return 4
    print(f"  [ok]   loaded {cls.__name__} (name={cls.name}); sha256={before[:12]}… unchanged")

    print("== [4/5] Polymarket connectivity ==")
    hosts = [_host(settings.clob_host), _host(settings.gamma_host)]
    if not _check_connectivity(hosts):
        print("  [FAIL] Polymarket API not reachable from this workspace")
        return 5

    print("== [5/5] maker / deposit wallet (read-only) ==")
    try:
        import asyncio

        from polybot.exchange.auth import build_clob_client
        from polybot.exchange.clob import ClobExchange
        from polybot.utils.retry import RetryPolicy
        from polybot.wallet.maker import MakerWalletValidator

        async def _check_maker() -> None:
            auth = await asyncio.to_thread(build_clob_client, settings)
            ex = ClobExchange(auth.client, signature_type=settings.signature_type,
                              retry_policy=RetryPolicy(attempts=2))
            validator = MakerWalletValidator(
                ex, signer=auth.address, funder=auth.funder,
                signature_type=settings.signature_type, api_key=auth.api_key,
                enable_live_trading=False)  # informational: never blocks preflight
            info = await validator.validate()
            print(f"  [ok]   signer={auth.address}")
            print(f"         maker/deposit wallet (used for ALL orders)={info.get('maker_address')}")
            print(f"         signature_type={info.get('signature_type')} ({info.get('signature_type_name')})")
            print(f"         L2 creds belong to signer account={info.get('creds_belong_to_account')}")
            print(f"         closed_only(ban)={info.get('closed_only')}")
            if info.get("authorized"):
                print("  [ok]   maker/creds coherent — NOTE: the CLOB enforces final maker "
                      "allow-listing at order time (deposit wallet must be enabled).")
            else:
                print("  [WARN] maker/deposit-wallet checks NOT fully satisfied — see execution.log; "
                      "live trading would be blocked until resolved.")

        asyncio.run(_check_maker())
    except Exception as exc:  # noqa: BLE001 - informational only; never fail preflight here
        print(f"  [WARN] maker/deposit-wallet check could not run (informational): {exc!r}")

    print("== PREFLIGHT PASSED ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
