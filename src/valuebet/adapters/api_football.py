"""Adapter de API-Football (API-Sports) — sólo CATÁLOGO (HU 1.3.1).

Implementa el Protocol `Adapter`. NO normaliza a `core` (eso es 1.3.2) ni toca
partidos/stats/cuotas (1.3.3+). Reusa el `HTTPClient` resiliente de la HU 1.1.2
(reintentos + rate limit); aquí NO se duplica esa lógica.

Sobre la envoltura del proveedor: la respuesta es
``{"get", "parameters", "errors", "results", "response": [...]}``. Un HTTP 200 con
``errors`` no vacío NO es éxito (p. ej. cuota/clave) → se trata como error de
aplicación (`ApiFootballError`) y se propaga (no se reintenta: llega en un 200).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import httpx

from valuebet.adapters.base import RawFetchResult
from valuebet.adapters.http import HTTPClient

BASE_URL = "https://v3.football.api-sports.io"
#: Tier gratuito ~10 req/min → intervalo mínimo de 6 s entre llamadas.
MIN_INTERVAL_SECONDS = 6.0
SOURCE_CODE = "api_sports"


class ApiFootballError(Exception):
    """Error de aplicación de API-Football (envoltura con `errors` o malformada).

    Distinto de los errores HTTP (5xx/4xx) que gestiona `HTTPClient`. Estos errores
    de cuota/clave llegan en un 200 y deben PROPAGARSE, nunca reintentarse.
    """

    def __init__(self, message: str, *, endpoint: str, errors: object | None = None) -> None:
        self.endpoint = endpoint
        self.errors = errors
        super().__init__(f"{message} (endpoint={endpoint}, errors={errors})")


def _ensure_application_ok(endpoint: str, data: Any) -> dict:
    """Valida la envoltura de API-Football; lanza `ApiFootballError` si no es OK."""
    if not isinstance(data, dict) or "response" not in data:
        raise ApiFootballError(
            "respuesta sin clave 'response'",
            endpoint=endpoint,
            errors=data.get("errors") if isinstance(data, dict) else None,
        )
    errors = data.get("errors")
    # `errors` es [] cuando todo va bien; un dict/list no vacío señala fallo
    # (cuota agotada, clave inválida, parámetro erróneo…).
    if errors:
        raise ApiFootballError("la API devolvió errores", endpoint=endpoint, errors=errors)
    return data


class ApiFootballAdapter:
    """Adapter de catálogo de API-Football. Recibe el `HTTPClient` ya configurado."""

    source_code = SOURCE_CODE

    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    def fetch(self, resource: str, params: Mapping[str, Any] | None = None) -> RawFetchResult:
        """Contrato `Adapter`: trae `resource` y devuelve el payload crudo + procedencia."""
        response = self._http.get(resource, params=params)
        data = _ensure_application_ok(resource, response.json())
        return RawFetchResult.build(
            source_code=self.source_code,
            endpoint=resource,
            payload=data,
            params=params,
            http_status=response.status_code,
        )

    # -- métodos de catálogo ------------------------------------------------
    def fetch_leagues(self, params: Mapping[str, Any] | None = None) -> RawFetchResult:
        """Catálogo de ligas y sus temporadas (endpoint /leagues)."""
        return self.fetch("/leagues", params)

    def fetch_teams(self, league_id: int, season: int) -> RawFetchResult:
        """Catálogo de equipos (con venue embebido) de una liga/temporada (/teams)."""
        return self.fetch("/teams", {"league": league_id, "season": season})


@contextmanager
def open_adapter(
    api_key: str,
    *,
    base_url: str = BASE_URL,
    timeout: float = 15.0,
    min_interval: float = MIN_INTERVAL_SECONDS,
) -> Iterator[ApiFootballAdapter]:
    """Crea un `ApiFootballAdapter` con un `HTTPClient` configurado y lo cierra al salir.

    La clave se inyecta SÓLO como header `x-apisports-key`; no se registra ni se
    expone de otro modo.
    """
    with httpx.Client(
        base_url=base_url,
        headers={"x-apisports-key": api_key},
        timeout=timeout,
    ) as inner:
        yield ApiFootballAdapter(HTTPClient(client=inner, min_interval=min_interval))
