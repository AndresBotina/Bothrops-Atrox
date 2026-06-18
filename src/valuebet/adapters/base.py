"""Contrato Adapter, agnóstico de proveedor.

Toda fuente (API, scraper, manual) implementa el mismo contrato: `fetch()` devuelve
un `RawFetchResult` con el payload crudo y su procedencia. Añadir una fuente nueva
sólo requiere implementar este Protocol; nada río abajo cambia (arquitectura §4).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


def compute_request_hash(endpoint: str, params: Mapping[str, Any] | None = None) -> str:
    """Hash determinista de (endpoint + params) para trazabilidad/dedupe.

    NO impone unicidad: en `raw` el mismo endpoint puede consultarse muchas veces
    (p. ej. cuotas) y cada consulta es una observación legítima distinta.
    """
    normalized = json.dumps(
        {"endpoint": endpoint, "params": dict(params or {})},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class RawFetchResult(BaseModel):
    """Resultado crudo de un fetch, listo para aterrizar en `raw.payloads`."""

    model_config = ConfigDict(frozen=True)

    source_code: str
    endpoint: str
    request_hash: str
    http_status: int | None = None
    payload: dict[str, Any]
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def build(
        cls,
        *,
        source_code: str,
        endpoint: str,
        payload: dict[str, Any],
        params: Mapping[str, Any] | None = None,
        http_status: int | None = None,
        fetched_at: datetime | None = None,
    ) -> RawFetchResult:
        """Construye un resultado calculando el `request_hash` desde endpoint+params."""
        return cls(
            source_code=source_code,
            endpoint=endpoint,
            request_hash=compute_request_hash(endpoint, params),
            http_status=http_status,
            payload=payload,
            fetched_at=fetched_at or datetime.now(UTC),
        )


@runtime_checkable
class Adapter(Protocol):
    """Contrato común a todas las fuentes de datos."""

    #: Código de la fuente en `meta.sources.code` (p. ej. 'api_sports').
    source_code: str

    def fetch(self, resource: str, params: Mapping[str, Any] | None = None) -> RawFetchResult:
        """Trae un recurso de la fuente y devuelve su payload crudo + procedencia."""
        ...
