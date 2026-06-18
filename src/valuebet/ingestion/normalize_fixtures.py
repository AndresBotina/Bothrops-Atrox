"""Normalización de PARTIDOS (raw /fixtures) hacia `core.matches` (HU 1.3.3).

Esta capa SÓLO lee de `raw.payloads` (el payload /fixtures más reciente) y escribe
en `core.matches`. No vuelve a la API ni toca `raw` (invariante #3). NO procesa
stats post-partido (1.3.4) ni cuotas (1.4).

Decisiones de diseño:
  * MAPEO DE ESTADOS explícito API-Football → core.match_statuses.code. Un status
    DESCONOCIDO no se fuerza a ningún valor: se omite el partido y se registra en
    `meta.data_quality_checks` (no metemos basura silenciosa).
  * RESULTADOS sólo en estados con resultado (finished/aet/penalties/abandoned/
    awarded). En el resto, los goles quedan NULL aunque la API mande algo. (Nota:
    `cancelled` es terminal en el esquema pero no porta resultado, así que tampoco
    recibe goles.)
  * IDENTIDAD: season/home/away/venue se resuelven desde `core.source_entity_map`.
    Si falta el catálogo (equipo/temporada no normalizados) NO se crea entidad
    fantasma: el partido se omite y se registra como no-normalizable.
  * IDEMPOTENCIA / ACTUALIZACIÓN DE ESTADO: el fixture.id se mapea como 'match';
    re-normalizar hace UPSERT (UPDATE de status/goles/kickoff), nunca duplica.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from valuebet.adapters.schemas.api_football import FixtureInfo, parse_fixtures
from valuebet.db.models.core import Match
from valuebet.db.models.meta import DataQualityCheck
from valuebet.db.models.raw import Payload
from valuebet.ingestion.normalize_catalog import (
    SOURCE_CODE,
    _get_source_id,
    _record_map,
    _resolve_via_map,
)
from valuebet.ingestion.raw import ingestion_run

# Mapeo explícito de fixture.status.short (API-Football) → core.match_statuses.code.
STATUS_MAP = {
    "NS": "scheduled",
    "TBD": "scheduled",
    "1H": "live",
    "2H": "live",
    "HT": "live",
    "ET": "live",
    "BT": "live",
    "P": "live",
    "LIVE": "live",
    "INT": "live",
    "SUSP": "live",
    "FT": "finished",
    "AET": "aet",
    "PEN": "penalties",
    "PST": "postponed",
    "CANC": "cancelled",
    "WO": "cancelled",
    "ABD": "abandoned",
    "AWD": "awarded",
}

# Estados cuyo resultado es válido (asignamos goles sólo en estos).
RESULT_BEARING_STATUSES = frozenset({"finished", "aet", "penalties", "abandoned", "awarded"})


@dataclass
class FixtureStats:
    """Conteo de partidos creados/actualizados/omitidos y sus incidencias."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    issues: list[dict] = field(default_factory=list)

    def skip(self, fixture_id: int, reason: str, **extra: object) -> None:
        self.skipped += 1
        self.issues.append({"fixture_id": fixture_id, "reason": reason, **extra})

    def as_details(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "issues": self.issues,
        }


def _kickoff_utc(fixture: FixtureInfo) -> datetime | None:
    """Deriva kickoff SIEMPRE en UTC desde timestamp (epoch) o date (ISO con tz)."""
    if fixture.timestamp is not None:
        return datetime.fromtimestamp(fixture.timestamp, tz=UTC)
    if fixture.date is not None:
        dt = fixture.date
        if dt.tzinfo is None:  # defensivo: si llegara naive, asumimos UTC
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return None


def _latest_fixtures_payload(session: Session, source_id: uuid.UUID) -> dict | None:
    return session.execute(
        select(Payload.payload)
        .where(Payload.source_id == source_id, Payload.endpoint == "/fixtures")
        .order_by(Payload.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _normalize_one(session: Session, source_id: uuid.UUID, entry, stats: FixtureStats) -> None:
    fx = entry.fixture
    short = fx.status.short
    code = STATUS_MAP.get(short)
    if code is None:
        # Status desconocido: no se fuerza a un valor; se omite y se registra.
        stats.skip(fx.id, "unknown_status", status_short=short)
        return

    # Resolución de identidad (el catálogo es prerequisito; sin fantasmas).
    season_ext = f"{entry.league.id}:{entry.league.season}"
    season_id = _resolve_via_map(session, source_id, "season", season_ext)
    home_id = _resolve_via_map(session, source_id, "team", str(entry.teams.home.id))
    away_id = _resolve_via_map(session, source_id, "team", str(entry.teams.away.id))

    missing: list[str] = []
    if season_id is None:
        missing.append(f"season:{season_ext}")
    if home_id is None:
        missing.append(f"team:{entry.teams.home.id}")
    if away_id is None:
        missing.append(f"team:{entry.teams.away.id}")
    if missing:
        stats.skip(fx.id, "missing_refs", missing=missing)
        return
    if home_id == away_id:
        stats.skip(fx.id, "home_equals_away")
        return

    kickoff = _kickoff_utc(fx)
    if kickoff is None:
        stats.skip(fx.id, "missing_kickoff")
        return

    venue_id = None
    if fx.venue.id is not None:
        # Venue opcional: si no está en el catálogo, se deja NULL (no se falla).
        venue_id = _resolve_via_map(session, source_id, "venue", str(fx.venue.id))

    terminal = code in RESULT_BEARING_STATUSES
    values = {
        "season_id": season_id,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "venue_id": venue_id,
        "status_code": code,
        "round": entry.league.round,
        "kickoff_utc": kickoff,
        "kickoff_tz": fx.timezone,
        "home_goals": entry.goals.home if terminal else None,
        "away_goals": entry.goals.away if terminal else None,
        "home_goals_ht": entry.score.halftime.home if terminal else None,
        "away_goals_ht": entry.score.halftime.away if terminal else None,
    }

    external_id = str(fx.id)
    mapped = _resolve_via_map(session, source_id, "match", external_id)
    if mapped is not None:
        match = session.get(Match, mapped)
        for key, value in values.items():
            setattr(match, key, value)
        session.flush()
        stats.updated += 1
    else:
        match = Match(**values)
        session.add(match)
        session.flush()
        _record_map(session, source_id, "match", external_id, match.id)
        stats.created += 1


def _record_quality_check(session: Session, run_id: uuid.UUID, stats: FixtureStats) -> None:
    session.add(
        DataQualityCheck(
            ingestion_run_id=run_id,
            check_name="fixture_normalization",
            severity="warning" if stats.issues else "info",
            passed=not stats.issues,
            details=stats.as_details(),
        )
    )
    session.flush()


def _normalize_fixtures_payload(
    session: Session, source_id: uuid.UUID, payload: dict, stats: FixtureStats
) -> None:
    """Proyecta un payload /fixtures a `core.matches` (sin abrir corrida propia).

    Reutilizable por el orquestador (HU 1.5.1) y por `normalize_fixtures`.
    """
    try:
        entries = parse_fixtures(payload)
    except ValidationError as exc:
        msg = f"payload de /fixtures no validó contra el esquema: {exc}"
        raise ValueError(msg) from exc
    for entry in entries:
        _normalize_one(session, source_id, entry, stats)


def normalize_fixtures(*, sessionmaker_=None) -> FixtureStats:
    """Normaliza los partidos desde el último raw /fixtures disponible.

    Lee el payload más reciente, valida con Pydantic y proyecta a `core.matches`
    resolviendo identidad. Registra la corrida en `meta.ingestion_runs` y un chequeo
    en `meta.data_quality_checks` (incluye los partidos omitidos y por qué).
    """
    stats = FixtureStats()
    with ingestion_run(SOURCE_CODE, "normalize_fixtures", sessionmaker_=sessionmaker_) as run:
        session = run.session
        source_id = _get_source_id(session)

        payload = _latest_fixtures_payload(session, source_id)
        if payload is not None:
            _normalize_fixtures_payload(session, source_id, payload, stats)

        run.rows_written = stats.created + stats.updated
        _record_quality_check(session, run.run_id, stats)

    return stats
