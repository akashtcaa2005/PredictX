"""Authentication & CLOB client construction.

Implements the documented Polymarket auth model on the **CLOB V2** client
(``py-clob-client-v2``):

* **L1** — the private key signs EIP-712 ``clobAuthTypedData`` to prove wallet
  ownership (handled inside the SDK). CLOB V2 signs orders against the bumped
  EIP-712 exchange domain (version ``2``); the legacy ``py-clob-client`` (v1)
  produced version-``1`` orders the exchange now rejects with
  ``invalid order version``.
* **L2** — API credentials (``apiKey``/``secret``/``passphrase``) authenticate
  every private CLOB request via ``POLY_*`` headers. We use explicit creds from
  the environment when provided, otherwise derive them from the key at startup.

Builder-program attribution changed in V2: it is expressed by an on-chain
builder **address** plus a 32-byte builder **code** embedded in the order
(``BuilderConfig``), not the legacy builder API creds. It is enabled only when
both ``POLY_BUILDER_ADDRESS`` and ``POLY_BUILDER_CODE`` are configured. See the
README for the credential-mapping assumptions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from polybot.config.settings import ConfigError, Settings


@dataclass
class AuthContext:
    """Everything downstream components need to talk to the CLOB.

    The L2 creds are held here (not logged) so the authenticated ``user``
    websocket can subscribe with them.
    """

    client: Any            # py_clob_client_v2.client.ClobClient
    address: str           # signer EOA address
    funder: str            # funder / proxy address orders settle against
    signature_type: int
    creds_source: str      # "explicit" | "derived"
    builder_enabled: bool
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""


def _import_sdk() -> Any:
    try:
        from py_clob_client_v2 import ClobClient  # noqa: WPS433 (lazy import)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ConfigError(
            "py-clob-client-v2 is not installed. Install dependencies first: "
            "`pip install -r requirements.txt`"
        ) from exc
    return ClobClient


def _build_builder_config(settings: Settings, log: logging.Logger) -> Any | None:
    """Best-effort construction of the V2 ``BuilderConfig`` for builder-program
    attribution.

    CLOB V2 attributes trades to a builder via an on-chain builder *address* and
    a 32-byte builder *code* carried in the order — not the legacy builder API
    creds (which now only authenticate the separate builder-reporting API).
    Attribution is enabled only when both are configured; otherwise it is
    disabled (strictly optional).
    """
    if not (settings.builder_address and settings.builder_code):
        return None
    try:
        from py_clob_client_v2 import BuilderConfig

        cfg = BuilderConfig(
            builder_address=settings.builder_address,
            builder_code=settings.builder_code,
        )
        log.info("builder config enabled")  # do not log any part of the builder key
        return cfg
    except Exception as exc:  # noqa: BLE001 - builder is strictly optional
        log.warning("builder config unavailable; continuing without it",
                    extra={"error": repr(exc)})
        return None


def build_clob_client(settings: Settings, logger: logging.Logger | None = None) -> AuthContext:
    """Construct, authenticate and return a ready-to-use CLOB V2 client.

    Raises :class:`ConfigError` if the SDK is missing or credentials cannot be
    established.
    """
    log = logger or logging.getLogger("polybot.exchange.auth")
    ClobClient = _import_sdk()
    from py_clob_client_v2 import ApiCreds

    builder_config = _build_builder_config(settings, log)

    explicit_creds = None
    if settings.api_key and settings.api_secret and settings.api_passphrase:
        explicit_creds = ApiCreds(
            api_key=settings.api_key,
            api_secret=settings.api_secret,
            api_passphrase=settings.api_passphrase,
        )

    client = ClobClient(
        host=settings.clob_host,
        chain_id=settings.chain_id,
        key=settings.private_key,
        creds=explicit_creds,
        signature_type=settings.signature_type,
        funder=settings.proxy_address or None,
        builder_config=builder_config,
    )

    address = client.get_address()

    if explicit_creds is not None:
        creds = explicit_creds
        client.set_api_creds(creds)
        creds_source = "explicit"
    else:
        # Derive L2 creds from the key (creates them server-side if absent). This
        # performs an L1-signed request and validates API connectivity.
        try:
            creds = client.create_or_derive_api_key()
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"failed to derive CLOB API credentials: {exc!r}") from exc
        client.set_api_creds(creds)
        creds_source = "derived"

    log.info(
        "clob client ready",
        extra={
            "address": address,
            "funder": settings.proxy_address,
            "signature_type": settings.signature_type,
            "creds_source": creds_source,
            "builder_enabled": builder_config is not None,
        },
    )
    return AuthContext(
        client=client,
        address=address,
        funder=settings.proxy_address,
        signature_type=settings.signature_type,
        creds_source=creds_source,
        builder_enabled=builder_config is not None,
        api_key=getattr(creds, "api_key", "") or "",
        api_secret=getattr(creds, "api_secret", "") or "",
        api_passphrase=getattr(creds, "api_passphrase", "") or "",
    )
