"""Async REST client (httpx) with retries, timeouts and HTTP-status handling.

Used for public/semi-public JSON endpoints (Gamma market data, the data-API
positions endpoint, and any optional external API such as Camber). Authenticated
CLOB *trading* goes through the official SDK in :mod:`polybot.exchange.clob`, not
here.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx

from polybot.monitoring.metrics import Metrics
from polybot.utils.retry import RetryPolicy, retry_async
from polybot.utils.time_utils import now_ms


class ApiError(RuntimeError):
    """Non-2xx HTTP response."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.body = body
        self.url = url


class AuthError(ApiError):
    """401 / 403 — do not retry."""


class RateLimitError(ApiError):
    """429 — retry with backoff."""


class ServerError(ApiError):
    """5xx — retry with backoff."""


class ClientError(ApiError):
    """Other 4xx — do not retry."""


# Transport failures + retryable HTTP errors.
_RETRYABLE = (httpx.TransportError, httpx.TimeoutException, RateLimitError, ServerError)
_FATAL = (AuthError, ClientError)


class RestClient:
    """Thin async wrapper over :class:`httpx.AsyncClient`."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        retry_policy: RetryPolicy | None = None,
        logger: logging.Logger | None = None,
        metrics: Metrics | None = None,
        name: str = "rest",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Accept": "application/json", **(headers or {})},
            timeout=timeout,
        )
        self._policy = retry_policy or RetryPolicy()
        self._log = logger or logging.getLogger("polybot.api")
        self._metrics = metrics
        self._name = name

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, *, params: Mapping[str, Any] | None = None,
                  headers: Mapping[str, str] | None = None) -> Any:
        return await self._request("GET", path, params=params, headers=headers)

    async def post(self, path: str, *, json: Any = None,
                   headers: Mapping[str, str] | None = None) -> Any:
        return await self._request("POST", path, json=json, headers=headers)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"

        async def _once() -> Any:
            start = now_ms()
            try:
                resp = await self._client.request(method, path, **kwargs)
            finally:
                if self._metrics:
                    self._metrics.observe_latency(f"{self._name}.latency", now_ms() - start)
            return self._handle(resp, url)

        def _on_retry(attempt: int, exc: BaseException, sleep: float) -> None:
            if self._metrics:
                self._metrics.incr(f"{self._name}.retry")
            self._log.warning("rest retry", extra={"api": self._name, "method": method,
                                                    "url": url, "attempt": attempt,
                                                    "error": repr(exc), "sleep_s": round(sleep, 2)})

        return await retry_async(
            _once, policy=self._policy, retry_on=_RETRYABLE, give_up_on=_FATAL,
            on_retry=_on_retry, description=f"{method} {url}",
        )

    def _handle(self, resp: httpx.Response, url: str) -> Any:
        status = resp.status_code
        if 200 <= status < 300:
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        body = resp.text
        if status in (401, 403):
            raise AuthError(status, body, url)
        if status == 429:
            raise RateLimitError(status, body, url)
        if status >= 500:
            raise ServerError(status, body, url)
        raise ClientError(status, body, url)
