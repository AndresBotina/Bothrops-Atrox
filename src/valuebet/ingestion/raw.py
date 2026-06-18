"""Zona raw: persistencia append-only de payloads y ciclo de vida de la corrida.

Invariante #3: `raw.payloads` es INMUTABLE. Aquí sólo se INSERTA, jamás UPDATE/DELETE.
No hay UNIQUE sobre `request_hash`: un mismo endpoint (p. ej. cuotas) se consulta
repetidamente y cada consulta es una fila nueva legítima. La idempotencia de
ENTIDADES se resuelve más adelante en core/market (UPSERT), no aquí.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from valuebet.adapters.base import RawFetchResult
from valuebet.db.models.meta import IngestionRun, Source
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_sessionmaker


def _resolve_source_id(session: Session, source_code: str) -> uuid.UUID:
    source_id = session.execute(
        select(Source.id).where(Source.code == source_code)
    ).scalar_one_or_none()
    if source_id is None:
        raise LookupError(
            f"fuente '{source_code}' no registrada en meta.sources; corre 'valuebet sources seed'"
        )
    return source_id


def persist_payload(
    session: Session,
    result: RawFetchResult,
    *,
    ingestion_run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Inserta (sólo INSERT) un RawFetchResult en `raw.payloads`. Devuelve su id."""
    payload = Payload(
        source_id=_resolve_source_id(session, result.source_code),
        ingestion_run_id=ingestion_run_id,
        endpoint=result.endpoint,
        request_hash=result.request_hash,
        http_status=result.http_status,
        payload=result.payload,
        fetched_at=result.fetched_at,
    )
    session.add(payload)
    session.flush()
    return payload.id


@dataclass
class RunHandle:
    """Asa de una corrida en curso: persiste payloads y lleva la cuenta de filas."""

    session: Session
    run_id: uuid.UUID
    rows_written: int = field(default=0)

    def persist(self, result: RawFetchResult) -> uuid.UUID:
        payload_id = persist_payload(self.session, result, ingestion_run_id=self.run_id)
        self.rows_written += 1
        return payload_id


@contextmanager
def ingestion_run(
    source_code: str,
    flow_name: str,
    *,
    params: dict[str, Any] | None = None,
    sessionmaker_: sessionmaker[Session] | None = None,
) -> Iterator[RunHandle]:
    """Abre una corrida en `meta.ingestion_runs` y la cierra según el desenlace.

    Apertura: status 'running' (commiteado de inmediato, para que la bitácora exista
    aunque el trabajo falle). Al salir bien: 'success' con `rows_written`. Ante
    excepción: 'failed' con el error y `finished_at`, y se re-lanza.
    """
    sm = sessionmaker_ or get_sessionmaker()
    session = sm()
    try:
        source_id = _resolve_source_id(session, source_code)
        run = IngestionRun(
            source_id=source_id,
            flow_name=flow_name,
            status="running",
            params=params or {},
        )
        session.add(run)
        session.commit()  # la corrida queda registrada pase lo que pase

        handle = RunHandle(session=session, run_id=run.id)
        try:
            yield handle
        except Exception as exc:
            session.rollback()  # descarta payloads no commiteados de esta corrida
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = datetime.now(UTC)
            session.add(run)
            session.commit()
            raise
        else:
            run.status = "success"
            run.rows_written = handle.rows_written
            run.finished_at = datetime.now(UTC)
            session.add(run)
            session.commit()
    finally:
        session.close()
