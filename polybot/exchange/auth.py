"""Authentication & CLOB client construction.

Implements the documented Polymarket auth model:

* **L1** — the private key signs EIP-712 ``clobAuthTypedData`` to prove wallet
  ownership (handled inside the SDK).
* **L2** — API credentials (``apiKey``/``secret``/``passphrase``) authenticate
  every private CLOB request via ``POLY_*`` headers. We use explicit creds from
  the environment when provided, otherwise derive them from the key at startup.

The ``POLY_BUILDER_*`` creds from ``env.txt`` are wired into the SDK's
``builder_config`` (builder-program attribution), which is distinct from the L2
trading creds. See the README for the credential-mapping assumptions.
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

    client: Any            # py_clob_client.client.ClobClient
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
        from py_clob_client.client import ClobClient  # noqa: WPS433 (lazy import)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ConfigError(
            "py-clob-client is not installed. Install dependencies first: "
            "`pip install -r requirements.txt`"
        ) from exc
    return ClobClient


def _build_builder_config(settings: Settings, log: logging.Logger) -> Any | None:
    """Best-effort construction of the SDK BuilderConfig from env creds."""
    if not (settings.builder_api_key and settings.builder_secret and settings.builder_passphrase):
        return None
    try:
        import inspect

        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

        params = set(inspect.signature(BuilderApiKeyCreds).parameters)
        candidates = {
            "api_key": settings.builder_api_key,
            "key": settings.builder_api_key,
            "secret": settings.builder_secret,
            "api_secret": settings.builder_secret,
            "passphrase": settings.builder_passphrase,
            "api_passphrase": settings.builder_passphrase,
        }
        kwargs = {k: v for k, v in candidates.items() if k in params}
        creds = BuilderApiKeyCreds(**kwargs)
        log.info("builder config enabled")  # do not log any part of the builder key
        return BuilderConfig(local_builder_creds=creds)
    except Exception as exc:  # noqa: BLE001 - builder is strictly optional
        log.warning("builder config unavailable; continuing without it",
                    extra={"error": repr(exc)})
        return None


def build_clob_client(settings: Settings, logger: logging.Logger | None = None) -> AuthContext:
    """Construct, authenticate and return a ready-to-use CLOB client.

    Raises :class:`ConfigError` if the SDK is missing or credentials cannot be
    established.
    """
    log = logger or logging.getLogger("polybot.exchange.auth")
    ClobClient = _import_sdk()
    from py_clob_client.clob_types import ApiCreds

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
            creds = client.create_or_derive_api_creds()
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
