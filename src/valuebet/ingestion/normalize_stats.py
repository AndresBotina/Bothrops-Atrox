"""Normalización de ESTADÍSTICAS post-partido (raw) hacia `core.match_team_stats`.

HU 1.3.4. Esta capa SÓLO lee de `raw.payloads` (el payload /fixtures/statistics más
reciente) y escribe en `core.match_team_stats`. No vuelve a la API ni toca `raw`
(invariante #3). NO procesa cuotas (1.4) ni orquestación (1.5).

Decisiones de diseño:
  * EXISTENCIA: sólo se normalizan stats de partidos que YA existen en core.matches
    y cuyo status porta resultado (terminal). match_id y team_id se resuelven desde
    `core.source_entity_map`; si faltan, se registra `missing_refs` y se omite (sin
    crear entidades fantasma).
  * AUSENCIA != CERO: un "type" ausente deja su columna en None (no 0). En especial
    xG: ausencia → None, nunca 0.0 (distinción clave para el modelo).
  * Posesión "55%" → 0.55 (fracción 0..1).
  * Sin datos → sin fila: si un equipo no trae stats usables, no se inserta una fila
    de puros NULL; se registra como `stats_ausentes`.
  * Una fila por equipo con is_home derivado del match; UNIQUE(match_id, team_id) →
    UPSERT idempotente.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from valuebet.adapters.schemas.api_football import StatItem, parse_statistics
from valuebet.db.models.core import Match, MatchTeamStats
from valuebet.db.models.meta import DataQualityCheck
from valuebet.db.models.raw import Payload
from valuebet.ingestion.normalize_catalog import (
    SOURCE_CODE,
    _get_source_id,
    _resolve_via_map,
)
from valuebet.ingestion.normalize_fixtures import RESULT_BEARING_STATUSES
from valuebet.ingestion.raw import ingestion_run

# "type" de API-Football → columna de core.match_team_stats.
_INT_TYPE_TO_COLUMN = {
    "Total Shots": "shots",
    "Shots on Goal": "shots_on_tgt",
    "Corner Kicks": "corners",
    "Fouls": "fouls",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
}
_POSSESSION_TYPE = "Ball Possession"
_XG_TYPE = "expected_goals"

# Columnas de stats que poblamos (xga se deriva luego: queda None por ahora).
_STAT_COLUMNS = (
    "shots",
    "shots_on_tgt",
    "possession",
    "corners",
    "fouls",
    "yellow_cards",
    "red_cards",
    "xg",
)


@dataclass
class StatsResult:
    """Conteo de filas insertadas/actualizadas vs partidos sin stats."""

    rows_upserted: int = 0
    matches_without_stats: int = 0
    skipped: int = 0
    issues: list[dict] = field(default_factory=list)

    def note(self, reason: str, **extra: object) -> None:
        self.issues.append({"reason": reason, **extra})

    def skip(self, reason: str, **extra: object) -> None:
        self.skipped += 1
        self.issues.append({"reason": reason, **extra})

    def as_details(self) -> dict:
        return {
            "rows_upserted": self.rows_upserted,
            "matches_without_stats": self.matches_without_stats,
            "skipped": self.skipped,
            "issues": self.issues,
        }


# --------------------------------------------------------------------------- #
# Parseo robusto de valores
# --------------------------------------------------------------------------- #
def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    """xG y demás floats. Ausencia/None → None (NUNCA 0.0)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_fraction(value: Any) -> float | None:
    """Posesión '55%' → 0.55 (fracción 0..1). None → None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        pct = float(value)
    else:
        text = str(value).strip().rstrip("%").strip()
        if not text or text.lower() == "null":
            return None
        try:
            pct = float(text)
        except ValueError:
            return None
    return pct / 100.0


def _parse_team_stats(statistics: list[StatItem]) -> dict[str, Any]:
    by_type = {item.type: item.value for item in statistics}
    parsed: dict[str, Any] = dict.fromkeys(_STAT_COLUMNS)
    for api_type, column in _INT_TYPE_TO_COLUMN.items():
        parsed[column] = _to_int(by_type.get(api_type))
    parsed["possession"] = _to_fraction(by_type.get(_POSSESSION_TYPE))
    # xG: ausencia → None (no 0.0). Si el "type" no está, by_type.get devuelve None.
    parsed["xg"] = _to_float(by_type.get(_XG_TYPE))
    return parsed


# --------------------------------------------------------------------------- #
# Persistencia (UPSERT por UNIQUE(match_id, team_id))
# --------------------------------------------------------------------------- #
def _upsert_team_stats(
    session: Session,
    match_id: uuid.UUID,
    team_id: uuid.UUID,
    is_home: bool,
    parsed: dict[str, Any],
    source_id: uuid.UUID,
) -> None:
    values = {
        "match_id": match_id,
        "team_id": team_id,
        "is_home": is_home,
        "source_id": source_id,
        **parsed,
    }
    stmt = pg_insert(MatchTeamStats).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["match_id", "team_id"],
        set_={"is_home": is_home, "source_id": source_id, **parsed},
    )
    session.execute(stmt)


# --------------------------------------------------------------------------- #
# Lectura de raw + orquestación
# --------------------------------------------------------------------------- #
def _latest_stats_payload(session: Session, source_id: uuid.UUID) -> dict | None:
    return session.execute(
        select(Payload.payload)
        .where(
            Payload.source_id == source_id,
            Payload.endpoint == "/fixtures/statistics",
        )
        .order_by(Payload.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _normalize_stats_payload(
    session: Session, source_id: uuid.UUID, payload: dict, result: StatsResult
) -> None:
    fixture_param = payload.get("parameters", {}).get("fixture")
    if fixture_param is None:
        result.skip("missing_fixture_param")
        return
    external_id = str(fixture_param)

    match_id = _resolve_via_map(session, source_id, "match", external_id)
    if match_id is None:
        # El partido no está en core: el catálogo/fixtures es prerequisito.
        result.skip("missing_refs", fixture_id=external_id, missing=["match"])
        return

    match = session.get(Match, match_id)
    if match.status_code not in RESULT_BEARING_STATUSES:
        # Un partido no terminal no tiene stats válidas.
        result.note("match_not_terminal", fixture_id=external_id, status=match.status_code)
        result.matches_without_stats += 1
        return

    try:
        team_entries = parse_statistics(payload)
    except ValidationError as exc:
        msg = f"payload de /fixtures/statistics no validó: {exc}"
        raise ValueError(msg) from exc

    if not team_entries:
        # Ausencia de stats: caso normal (liga sin cobertura / partido viejo).
        result.note("stats_ausentes", fixture_id=external_id)
        result.matches_without_stats += 1
        return

    inserted_any = False
    for entry in team_entries:
        team_id = _resolve_via_map(session, source_id, "team", str(entry.team.id))
        if team_id is None:
            result.skip("missing_refs", fixture_id=external_id, missing=[f"team:{entry.team.id}"])
            continue
        if team_id == match.home_team_id:
            is_home = True
        elif team_id == match.away_team_id:
            is_home = False
        else:
            result.skip("team_not_in_match", fixture_id=external_id, team=entry.team.id)
            continue

        parsed = _parse_team_stats(entry.statistics)
        if all(parsed[col] is None for col in _STAT_COLUMNS):
            # Sin datos usables: no insertamos una fila de puros NULL.
            result.note("stats_ausentes_equipo", fixture_id=external_id, team=entry.team.id)
            continue

        _upsert_team_stats(session, match_id, team_id, is_home, parsed, source_id)
        result.rows_upserted += 1
        inserted_any = True

    if not inserted_any:
        result.matches_without_stats += 1


def _record_quality_check(session: Session, run_id: uuid.UUID, result: StatsResult) -> None:
    session.add(
        DataQualityCheck(
            ingestion_run_id=run_id,
            check_name="stats_normalization",
            severity="warning" if result.skipped else "info",
            passed=result.skipped == 0,
            details=result.as_details(),
        )
    )
    session.flush()


def normalize_stats(*, sessionmaker_=None) -> StatsResult:
    """Normaliza las estadísticas desde el último raw /fixtures/statistics disponible.

    Registra la corrida en `meta.ingestion_runs` y un chequeo en
    `meta.data_quality_checks` (filas insertadas vs partidos sin stats).
    """
    result = StatsResult()
    with ingestion_run(SOURCE_CODE, "normalize_stats", sessionmaker_=sessionmaker_) as run:
        session = run.session
        source_id = _get_source_id(session)

        payload = _latest_stats_payload(session, source_id)
        if payload is not None:
            _normalize_stats_payload(session, source_id, payload, result)

        run.rows_written = result.rows_upserted
        _record_quality_check(session, run.run_id, result)

    return result
