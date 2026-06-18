"""Normalización del CATÁLOGO crudo (raw) hacia entidades `core` (HU 1.3.2).

Esta capa SÓLO lee de `raw.payloads` (el payload más reciente de /leagues y /teams)
y escribe en `core`. No vuelve a la API ni toca `raw` (invariante #3). No procesa
partidos/stats/cuotas (eso es 1.3.3+).

Idempotencia (invariante #5) por DOS mecanismos combinados:
  1. `core.source_entity_map`: mapea el external_id de API-Football → UUID interno.
     Primer mecanismo de resolución de identidad.
  2. Claves naturales del esquema (segunda red): país por iso_code/nombre,
     competición por (sport, country, name, kind), temporada por (competition, label).
Los países NO se mapean (el enum de `entity_type` no los incluye): se resuelven por
clave natural.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from valuebet.adapters.schemas.api_football import (
    League,
    Season,
    Team,
    Venue,
    parse_leagues,
    parse_teams,
)
from valuebet.db.models.core import (
    Competition,
    Country,
    SourceEntityMap,
    Sport,
)
from valuebet.db.models.core import Season as SeasonModel
from valuebet.db.models.core import Team as TeamModel
from valuebet.db.models.core import Venue as VenueModel
from valuebet.db.models.meta import DataQualityCheck, Source
from valuebet.db.models.raw import Payload
from valuebet.ingestion.raw import ingestion_run

SOURCE_CODE = "api_sports"
SPORT_CODE = "football"

# "type" de API-Football (League/Cup) -> nuestro kind (CHECK league/cup/...).
_KIND_BY_API_TYPE = {"league": "league", "cup": "cup"}


@dataclass
class NormalizeStats:
    """Conteo de entidades nuevas vs resueltas (chequeo simple de la corrida)."""

    created: int = 0
    resolved: int = 0
    created_by_type: Counter[str] = field(default_factory=Counter)

    def mark(self, entity_type: str, created: bool) -> None:
        if created:
            self.created += 1
            self.created_by_type[entity_type] += 1
        else:
            self.resolved += 1

    def as_details(self) -> dict:
        return {
            "created": self.created,
            "resolved": self.resolved,
            "created_by_type": dict(self.created_by_type),
        }


# --------------------------------------------------------------------------- #
# Resolución de identidad
# --------------------------------------------------------------------------- #
def _resolve_via_map(
    session: Session, source_id: uuid.UUID, entity_type: str, external_id: str
) -> uuid.UUID | None:
    return session.execute(
        select(SourceEntityMap.internal_id).where(
            SourceEntityMap.source_id == source_id,
            SourceEntityMap.entity_type == entity_type,
            SourceEntityMap.external_id == external_id,
        )
    ).scalar_one_or_none()


def _record_map(
    session: Session,
    source_id: uuid.UUID,
    entity_type: str,
    external_id: str,
    internal_id: uuid.UUID,
) -> None:
    stmt = (
        pg_insert(SourceEntityMap)
        .values(
            source_id=source_id,
            entity_type=entity_type,
            external_id=external_id,
            internal_id=internal_id,
        )
        .on_conflict_do_nothing(index_elements=["source_id", "entity_type", "external_id"])
    )
    session.execute(stmt)


# --------------------------------------------------------------------------- #
# Bootstrap de la jerarquía deportiva
# --------------------------------------------------------------------------- #
def _ensure_sport(session: Session) -> uuid.UUID:
    stmt = (
        pg_insert(Sport)
        .values(code=SPORT_CODE, name="Football")
        .on_conflict_do_update(index_elements=["code"], set_={"name": "Football"})
        .returning(Sport.id)
    )
    return session.execute(stmt).scalar_one()


def _get_source_id(session: Session) -> uuid.UUID:
    source_id = session.execute(
        select(Source.id).where(Source.code == SOURCE_CODE)
    ).scalar_one_or_none()
    if source_id is None:
        raise LookupError(f"fuente '{SOURCE_CODE}' no registrada; corre 'valuebet sources seed'")
    return source_id


# --------------------------------------------------------------------------- #
# Upserts por entidad (mapa de identidad + clave natural)
# --------------------------------------------------------------------------- #
def _upsert_country(
    session: Session, name: str, code: str | None, stats: NormalizeStats
) -> uuid.UUID:
    # Países sin entrada en source_entity_map: resolución por clave natural.
    if code:
        existing = session.execute(
            select(Country.id).where(Country.iso_code == code)
        ).scalar_one_or_none()
    else:
        existing = session.execute(
            select(Country.id).where(Country.name == name, Country.iso_code.is_(None))
        ).scalar_one_or_none()
    if existing is not None:
        stats.mark("country", False)
        return existing
    country = Country(name=name, iso_code=code)
    session.add(country)
    session.flush()
    stats.mark("country", True)
    return country.id


def _upsert_competition(
    session: Session,
    source_id: uuid.UUID,
    sport_id: uuid.UUID,
    league: League,
    country_id: uuid.UUID | None,
    stats: NormalizeStats,
) -> uuid.UUID:
    external_id = str(league.id)
    mapped = _resolve_via_map(session, source_id, "competition", external_id)
    if mapped is not None:
        stats.mark("competition", False)
        return mapped

    kind = _KIND_BY_API_TYPE.get((league.type or "").lower(), "league")
    conditions = [
        Competition.sport_id == sport_id,
        Competition.name == league.name,
        Competition.kind == kind,
        Competition.country_id == country_id
        if country_id is not None
        else Competition.country_id.is_(None),
    ]
    existing = session.execute(select(Competition.id).where(*conditions)).scalar_one_or_none()
    if existing is not None:
        _record_map(session, source_id, "competition", external_id, existing)
        stats.mark("competition", False)
        return existing

    competition = Competition(sport_id=sport_id, country_id=country_id, name=league.name, kind=kind)
    session.add(competition)
    session.flush()
    _record_map(session, source_id, "competition", external_id, competition.id)
    stats.mark("competition", True)
    return competition.id


def _upsert_season(
    session: Session,
    source_id: uuid.UUID,
    league_external_id: str,
    competition_id: uuid.UUID,
    season: Season,
    stats: NormalizeStats,
) -> uuid.UUID:
    label = str(season.year)
    external_id = f"{league_external_id}:{season.year}"
    mapped = _resolve_via_map(session, source_id, "season", external_id)
    if mapped is not None:
        stats.mark("season", False)
        return mapped

    existing = session.execute(
        select(SeasonModel.id).where(
            SeasonModel.competition_id == competition_id, SeasonModel.label == label
        )
    ).scalar_one_or_none()
    if existing is not None:
        _record_map(session, source_id, "season", external_id, existing)
        stats.mark("season", False)
        return existing

    row = SeasonModel(
        competition_id=competition_id,
        label=label,
        start_date=season.start,
        end_date=season.end,
        is_current=bool(season.current),
    )
    session.add(row)
    session.flush()
    _record_map(session, source_id, "season", external_id, row.id)
    stats.mark("season", True)
    return row.id


def _upsert_venue(
    session: Session, source_id: uuid.UUID, venue: Venue, stats: NormalizeStats
) -> uuid.UUID | None:
    # Venue opcional: si la API no da id/nombre, no podemos identificarlo → se omite.
    if venue.id is None or not venue.name:
        return None
    external_id = str(venue.id)
    mapped = _resolve_via_map(session, source_id, "venue", external_id)
    if mapped is not None:
        stats.mark("venue", False)
        return mapped

    row = VenueModel(name=venue.name, city=venue.city)
    session.add(row)
    session.flush()
    _record_map(session, source_id, "venue", external_id, row.id)
    stats.mark("venue", True)
    return row.id


def _upsert_team(
    session: Session,
    source_id: uuid.UUID,
    sport_id: uuid.UUID,
    team: Team,
    home_venue_id: uuid.UUID | None,
    stats: NormalizeStats,
) -> uuid.UUID:
    external_id = str(team.id)
    mapped = _resolve_via_map(session, source_id, "team", external_id)
    if mapped is not None:
        stats.mark("team", False)
        return mapped

    row = TeamModel(
        sport_id=sport_id,
        name=team.name,
        short_name=team.code,
        founded_year=team.founded,
        home_venue_id=home_venue_id,
    )
    session.add(row)
    session.flush()
    _record_map(session, source_id, "team", external_id, row.id)
    stats.mark("team", True)
    return row.id


# --------------------------------------------------------------------------- #
# Lectura de raw + orquestación
# --------------------------------------------------------------------------- #
def _latest_payload(session: Session, source_id: uuid.UUID, endpoint: str) -> dict | None:
    return session.execute(
        select(Payload.payload)
        .where(Payload.source_id == source_id, Payload.endpoint == endpoint)
        .order_by(Payload.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _normalize_leagues(
    session: Session,
    source_id: uuid.UUID,
    sport_id: uuid.UUID,
    payload: dict,
    stats: NormalizeStats,
) -> None:
    try:
        entries = parse_leagues(payload)
    except ValidationError as exc:
        raise ValueError(f"payload de /leagues no validó contra el esquema: {exc}") from exc

    for entry in entries:
        country_id = _upsert_country(session, entry.country.name, entry.country.code, stats)
        competition_id = _upsert_competition(
            session, source_id, sport_id, entry.league, country_id, stats
        )
        for season in entry.seasons:
            _upsert_season(session, source_id, str(entry.league.id), competition_id, season, stats)


def _normalize_teams(
    session: Session,
    source_id: uuid.UUID,
    sport_id: uuid.UUID,
    payload: dict,
    stats: NormalizeStats,
) -> None:
    try:
        entries = parse_teams(payload)
    except ValidationError as exc:
        raise ValueError(f"payload de /teams no validó contra el esquema: {exc}") from exc

    for entry in entries:
        venue_id = _upsert_venue(session, source_id, entry.venue, stats)
        _upsert_team(session, source_id, sport_id, entry.team, venue_id, stats)


def _record_quality_check(session: Session, run_id: uuid.UUID, stats: NormalizeStats) -> None:
    session.add(
        DataQualityCheck(
            ingestion_run_id=run_id,
            check_name="catalog_normalization",
            severity="info",
            passed=True,
            details=stats.as_details(),
        )
    )
    session.flush()


def normalize_catalog(*, sessionmaker_=None) -> NormalizeStats:
    """Normaliza el catálogo desde el último raw disponible. Devuelve los conteos.

    Lee el payload más reciente de /leagues y /teams (si existen), valida con Pydantic
    y proyecta a `core` resolviendo identidad. Registra la corrida en
    `meta.ingestion_runs` y un chequeo en `meta.data_quality_checks`.
    """
    stats = NormalizeStats()
    with ingestion_run(SOURCE_CODE, "normalize_catalog", sessionmaker_=sessionmaker_) as run:
        session = run.session
        source_id = _get_source_id(session)
        sport_id = _ensure_sport(session)

        leagues_payload = _latest_payload(session, source_id, "/leagues")
        if leagues_payload is not None:
            _normalize_leagues(session, source_id, sport_id, leagues_payload, stats)

        teams_payload = _latest_payload(session, source_id, "/teams")
        if teams_payload is not None:
            _normalize_teams(session, source_id, sport_id, teams_payload, stats)

        run.rows_written = stats.created
        _record_quality_check(session, run.run_id, stats)

    return stats
