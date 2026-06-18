"""Cliente HTTP resiliente: timeouts, reintentos con backoff y límite de tasa.

Política de reintentos (HU 1.1.2):
  * Reintenta SÓLO en errores transitorios: 5xx y errores de transporte/timeout.
  * NUNCA reintenta en 4xx: son definitivos y se propagan con contexto (url, status).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class HTTPError(Exception):
    """Error HTTP con contexto de la petición."""

    def __init__(self, message: str, *, url: str, status: int | None = None) -> None:
        self.url = url
        self.status = status
        super().__init__(f"{message} (url={url}, status={status})")


class TransientHTTPError(HTTPError):
    """Error transitorio (5xx): elegible para reintento."""


class DefinitiveHTTPError(HTTPError):
    """Error definitivo (4xx): NO se reintenta, se propaga."""


# Excepciones que disparan reintento: transitorias propias + transporte de httpx
# (httpx.TimeoutException y httpx.NetworkError heredan de httpx.TransportError).
_RETRYABLE = (TransientHTTPError, httpx.TransportError)


class HTTPClient:
    """Envoltura fina sobre `httpx.Client` con reintentos y límite de tasa."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        min_interval: float = 0.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self._last_call_monotonic: float | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    # -- ciclo de vida ------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HTTPClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- API pública --------------------------------------------------------
    def get(self, url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        return self.request("GET", url, params=params)

    def request(
        self, method: str, url: str, params: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        retryer = Retrying(
            retry=retry_if_exception_type(_RETRYABLE),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=self.backoff_base),
            reraise=True,
        )
        return retryer(self._do_request, method, url, params)

    # -- internos -----------------------------------------------------------
    def _respect_rate_limit(self) -> None:
        if self.min_interval <= 0 or self._last_call_monotonic is None:
            self._last_call_monotonic = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_call_monotonic
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_call_monotonic = time.monotonic()

    def _do_request(
        self, method: str, url: str, params: Mapping[str, Any] | None
    ) -> httpx.Response:
        self._respect_rate_limit()
        # Los errores de transporte/timeout se propagan y tenacity los reintenta.
        response = self._client.request(method, url, params=dict(params or {}))

        status = response.status_code
        if status >= 500:
            raise TransientHTTPError(
                "respuesta 5xx del servidor", url=str(response.request.url), status=status
            )
        if status >= 400:
            raise DefinitiveHTTPError(
                "respuesta 4xx (definitiva)", url=str(response.request.url), status=status
            )
        return response
