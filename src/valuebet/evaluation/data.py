"""Carga de partidos desde `core` hacia objetos de dominio `Match` (HU 2.1).

Capa de I/O del evaluador: traduce filas de `core.matches` a `Match` puros para
que `backtest.py` y `metrics.py` no conozcan la base. Sólo LECTURA sobre `core`.

El `--league` de la CLI es el id externo de API-Football (39, 140…). Se resuelve
a la competición interna vía `core.source_entity_map` (entity_type='competition'),
el mismo mecanismo de identidad que usa la ingesta. Sólo se cargan partidos con
resultado limpio (goles no nulos y estado con resultado), que son los evaluables.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from valuebet.db.models.core import Match as MatchModel
from valuebet.db.models.core import Season
from valuebet.db.session import get_session
from valuebet.evaluation.model import Match
from valuebet.ingestion.normalize_catalog import SOURCE_CODE, _get_source_id, _resolve_via_map

# Estados que portan un resultado 1X2 limpio (se excluye 'abandoned': terminal
# pero sin resultado fiable). El filtro real es goles no nulos; esto lo refuerza.
RESULT_STATUSES = ("finished", "aet", "penalties", "awarded")


def resolve_competition_id(session: Session, league_external_id: int):
    """UUID de la competición interna para un id de liga de API-Football."""
    source_id = _get_source_id(session)
    competition_id = _resolve_via_map(session, source_id, "competition", str(league_external_id))
    if competition_id is None:
        raise LookupError(
            f"liga {league_external_id} no encontrada en core (vía source_entity_map); "
            f"¿ingestaste esa liga? Fuente '{SOURCE_CODE}'."
        )
    return competition_id


def load_matches(league_external_id: int, *, from_season: int | None = None) -> list[Match]:
    """Carga los partidos evaluables de una liga como `Match` de dominio.

    Args:
        league_external_id: id de liga de API-Football (p. ej. 39 = Premier).
        from_season: si se da, sólo temporadas cuya etiqueta es >= ese año
            (las etiquetas son años de 4 dígitos, p. ej. '2023').
    """
    with get_session() as session:
        competition_id = resolve_competition_id(session, league_external_id)

        stmt = (
            select(MatchModel)
            .join(Season, Season.id == MatchModel.season_id)
            .where(
                Season.competition_id == competition_id,
                MatchModel.status_code.in_(RESULT_STATUSES),
                MatchModel.home_goals.is_not(None),
                MatchModel.away_goals.is_not(None),
            )
            .order_by(MatchModel.kickoff_utc)
        )
        if from_season is not None:
            stmt = stmt.where(Season.label >= str(from_season))

        rows = session.execute(stmt).scalars().all()
        return [
            Match(
                match_id=row.id,
                kickoff_utc=row.kickoff_utc,
                home_team_id=row.home_team_id,
                away_team_id=row.away_team_id,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
            )
            for row in rows
        ]
