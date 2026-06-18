"""Registro de fuentes en `meta.sources` (bootstrap idempotente).

Esto NO es una migración de esquema: la migración crea la TABLA; aquí sembramos
FILAS de registro. La elección concreta de proveedor queda abierta; cambiarla luego
es simplemente otro UPSERT por `code`.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from valuebet.db.models.meta import Source
from valuebet.db.session import get_session

# Fuentes previstas. Filas de registro; la activación real de cada proveedor se
# decide más adelante.
KNOWN_SOURCES: tuple[dict[str, str | bool], ...] = (
    {"code": "api_sports", "name": "API-Sports (fútbol)", "kind": "api"},
    {"code": "the_odds_api", "name": "The Odds API (cuotas)", "kind": "api"},
)


def seed_sources(session: Session | None = None) -> int:
    """UPSERT idempotente por `code` en `meta.sources`. Devuelve cuántas filas sembró.

    Re-ejecutarlo no duplica filas: actualiza `name`/`kind` de las existentes.
    """
    if session is not None:
        return _seed(session, KNOWN_SOURCES)
    with get_session() as managed:
        return _seed(managed, KNOWN_SOURCES)


def _seed(session: Session, rows: tuple[dict[str, str | bool], ...]) -> int:
    stmt = insert(Source).values(list(rows))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Source.code],
        set_={"name": stmt.excluded.name, "kind": stmt.excluded.kind},
    )
    session.execute(stmt)
    return len(rows)
