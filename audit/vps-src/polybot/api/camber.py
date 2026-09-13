"""Optional Camber API integration — token supplied via environment only.

Per the deployment brief, if a Camber API token is configured it must be read
from the environment (never hardcoded) and sent as a secure ``Authorization:
Bearer`` header. When no token is configured this module is inert: the bot runs
normally without any Camber dependency.
"""
from __future__ import annotations

import logging
from typing import Any

from polybot.api.rest_client import RestClient
from polybot.config.settings import Settings
from polybot.monitoring.metrics import Metrics
from polybot.utils.retry import RetryPolicy


class CamberClient:
    """Authenticated client for the optional Camber API."""

    def __init__(self, token: str, base_url: str, *, timeout: float = 10.0,
                 retry_policy: RetryPolicy | None = None, logger: logging.Logger | None = None,
                 metrics: Metrics | None = None) -> None:
        self._rest = RestClient(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            retry_policy=retry_policy,
            logger=logger or logging.getLogger("polybot.api.camber"),
            metrics=metrics,
            name="camber",
        )

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._rest.get(path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self._rest.post(path, **kwargs)

    async def aclose(self) -> None:
        await self._rest.aclose()

    @classmethod
    def from_settings(cls, settings: Settings, *, metrics: Metrics | None = None,
                      logger: logging.Logger | None = None) -> "CamberClient | None":
        """Build a client iff a token *and* URL are configured; else ``None``."""
        if not settings.camber_api_token or not settings.camber_api_url:
            return None
        return cls(
            token=settings.camber_api_token,
            base_url=settings.camber_api_url,
            timeout=settings.http_timeout,
            retry_policy=RetryPolicy(
                attempts=settings.retry_max_attempts,
                base_delay=settings.retry_base_delay,
                max_delay=settings.retry_max_delay,
            ),
            logger=logger,
            metrics=metrics,
        )
