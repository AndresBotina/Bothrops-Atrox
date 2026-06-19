"""Backfill histórico REANUDABLE sobre varias (liga, temporada) — HU 1.5.2.

Orquesta `ingest_league_season` (1.5.1) EN LOTE; no reescribe la ingesta de una
league-season. Propiedades:

  * REANUDABILIDAD desde el ESTADO REAL en core (no un archivo de cursor frágil):
    al arrancar, cada (liga, temporada) se evalúa leyendo la base y se decide si está
    completa, parcial/pendiente o sin datos. Una corrida posterior recalcula y
    continúa por donde quedó.
  * TOPE DURO de seguridad: `request_budget` GLOBAL compartido por todo el lote. Se
    reparte entre las league-seasons; al agotarse, el backfill se detiene limpio
    (parcial) dejando el resto pendiente. NUNCA se excede.
  * IDEMPOTENCIA + skip_existing: lo ya completo no consume peticiones; retomar no
    duplica ni re-fetchea (lo garantiza la 1.5.1).
  * PROFUNDIDAD VARIABLE: una temporada sin datos para una liga (fixtures vacíos) NO
    es error: se registra (`temporada_sin_datos`) y el lote sigue.

CRITERIO DE COMPLETITUD (explícito y verificable desde core):
  Una (liga, temporada) está COMPLETA cuando:
    1. Catálogo presente: la temporada está mapeada en `core.source_entity_map`
       (es decir, /leagues + /teams se normalizaron).
    2. Fixtures presentes: existe ≥1 partido en `core.matches` para esa temporada.
    3. Stats cubiertas: todo partido TERMINAL tiene filas en `core.match_team_stats`
       O un marcador `match_no_stats` (registrado cuando se intentó traer sus stats
       y la API no las trae). (Una temporada cuyos partidos son todos no-terminales
       cumple trivialmente el punto 3.)
  Es SIN_DATOS si existe un marcador `temporada_sin_datos`. En cualquier otro caso
  es PENDIENTE/PARCIAL.

AUDITORÍA (relación run padre / sub-runs):
  Una corrida padre (flow 'backfill') agrupa el lote en `meta.ingestion_runs`. Cada
  `ingest_league_season` abre su PROPIA sub-run (flow 'ingest_league_season'); el
  resumen del backfill registra, por target, el id de su sub-run, de modo que la
  trazabilidad queda enlazada (meta.ingestion_runs no tiene FK padre→hijo).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from valuebet.adapters.api_football import ApiFootballAdapter
from valuebet.db.models.core import Match, MatchTeamStats
from valuebet.db.models.meta import DataQualityCheck
from valuebet.ingestion.normalize_catalog import SOURCE_CODE, _get_source_id, _resolve_via_map
from valuebet.ingestion.normalize_fixtures import RESULT_BEARING_STATUSES
from valuebet.ingestion.orchestrate import ingest_league_season
from valuebet.ingestion.raw import ingestion_run

NO_DATA_CHECK = "temporada_sin_datos"
NO_STATS_CHECK = "match_no_stats"


# --------------------------------------------------------------------------- #
# Lectura de estado desde core (reanudabilidad)
# --------------------------------------------------------------------------- #
def _season_id(session: Session, source_id: uuid.UUID, league_id: int, season: int):
    return _resolve_via_map(session, source_id, "season", f"{league_id}:{season}")


def _n_matches(session: Session, season_id: uuid.UUID) -> int:
    return int(
        session.scalar(select(func.count()).select_from(Match).where(Match.season_id == season_id))
    )


def _n_stats_rows(session: Session, season_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(MatchTeamStats)
            .join(Match, Match.id == MatchTeamStats.match_id)
            .where(Match.season_id == season_id)
        )
    )


def _terminal_match_ids(session: Session, season_id: uuid.UUID) -> set[uuid.UUID]:
    rows = session.execute(
        select(Match.id).where(
            Match.season_id == season_id, Match.status_code.in_(RESULT_BEARING_STATUSES)
        )
    ).scalars()
    return set(rows)


def _match_ids_with_stats(session: Session, season_id: uuid.UUID) -> set[uuid.UUID]:
    rows = session.execute(
        select(MatchTeamStats.match_id)
        .join(Match, Match.id == MatchTeamStats.match_id)
        .where(Match.season_id == season_id)
        .distinct()
    ).scalars()
    return set(rows)


def _no_stats_marked_ids(session: Session) -> set[uuid.UUID]:
    rows = session.execute(
        select(DataQualityCheck.details).where(DataQualityCheck.check_name == NO_STATS_CHECK)
    ).scalars()
    out: set[uuid.UUID] = set()
    for details in rows:
        match_id = (details or {}).get("match_id")
        if match_id:
            out.add(uuid.UUID(match_id))
    return out


def _has_no_data_marker(session: Session, league_id: int, season: int) -> bool:
    found = session.execute(
        select(DataQualityCheck.id)
        .where(
            DataQualityCheck.check_name == NO_DATA_CHECK,
            DataQualityCheck.details["league"].astext == str(league_id),
            DataQualityCheck.details["season"].astext == str(season),
        )
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


@dataclass
class TargetState:
    league_id: int
    season: int
    has_catalog: bool
    n_matches: int
    n_terminal: int
    n_handled: int
    no_data: bool

    @property
    def state(self) -> str:
        if self.no_data:
            return "no_data"
        if not self.has_catalog or self.n_matches == 0:
            return "pending"
        if self.n_handled >= self.n_terminal:
            return "complete"
        return "partial"


def evaluate_target(
    session: Session, source_id: uuid.UUID, league_id: int, season: int
) -> TargetState:
    """Deriva el estado de una (liga, temporada) leyendo SÓLO la base (core + marcadores)."""
    if _has_no_data_marker(session, league_id, season):
        return TargetState(league_id, season, False, 0, 0, 0, no_data=True)
    season_id = _season_id(session, source_id, league_id, season)
    if season_id is None:
        return TargetState(league_id, season, False, 0, 0, 0, no_data=False)
    terminal = _terminal_match_ids(session, season_id)
    handled = terminal & (_match_ids_with_stats(session, season_id) | _no_stats_marked_ids(session))
    return TargetState(
        league_id,
        season,
        has_catalog=True,
        n_matches=_n_matches(session, season_id),
        n_terminal=len(terminal),
        n_handled=len(handled),
        no_data=False,
    )


# --------------------------------------------------------------------------- #
# Marcadores (registro en data_quality_checks)
# --------------------------------------------------------------------------- #
def _mark_no_data(session: Session, run_id: uuid.UUID, league_id: int, season: int) -> None:
    if _has_no_data_marker(session, league_id, season):
        return
    session.add(
        DataQualityCheck(
            ingestion_run_id=run_id,
            check_name=NO_DATA_CHECK,
            severity="info",
            passed=True,
            details={"league": league_id, "season": season},
        )
    )
    session.flush()


def _mark_terminal_without_stats(session: Session, run_id: uuid.UUID, season_id: uuid.UUID) -> None:
    terminal = _terminal_match_ids(session, season_id)
    handled = _match_ids_with_stats(session, season_id) | _no_stats_marked_ids(session)
    for match_id in terminal - handled:
        session.add(
            DataQualityCheck(
                ingestion_run_id=run_id,
                check_name=NO_STATS_CHECK,
                severity="info",
                passed=True,
                details={"match_id": str(match_id)},
            )
        )
    session.flush()


# --------------------------------------------------------------------------- #
# Resumen
# --------------------------------------------------------------------------- #
@dataclass
class TargetResult:
    league_id: int
    season: int
    state: str  # complete | partial | pending | no_data
    n_matches: int
    n_stats_rows: int
    requests: int
    run_id: str | None = None


@dataclass
class BackfillSummary:
    targets: list[TargetResult] = field(default_factory=list)
    requests_made: int = 0
    budget_exhausted: bool = False
    status: str = "success"

    def as_details(self) -> dict:
        return {
            "requests_made": self.requests_made,
            "budget_exhausted": self.budget_exhausted,
            "status": self.status,
            "targets": [
                {
                    "league": t.league_id,
                    "season": t.season,
                    "state": t.state,
                    "n_matches": t.n_matches,
                    "n_stats_rows": t.n_stats_rows,
                    "requests": t.requests,
                    "run_id": t.run_id,
                }
                for t in self.targets
            ],
        }


def _build_result(
    session: Session,
    source_id: uuid.UUID,
    league_id: int,
    season: int,
    state: str,
    requests: int,
    run_id: uuid.UUID | None = None,
) -> TargetResult:
    season_id = _season_id(session, source_id, league_id, season)
    n_matches = _n_matches(session, season_id) if season_id else 0
    n_stats = _n_stats_rows(session, season_id) if season_id else 0
    return TargetResult(
        league_id, season, state, n_matches, n_stats, requests, str(run_id) if run_id else None
    )


def run_backfill(
    adapter: ApiFootballAdapter,
    targets: list[tuple[int, int]],
    *,
    request_budget: int | None = None,
    skip_existing: bool = True,
    sessionmaker_=None,
) -> BackfillSummary:
    """Recorre `targets` (lista de (league_id, season)) acumulando histórico.

    Reanudable e idempotente; respeta `request_budget` como tope DURO global.
    """
    summary = BackfillSummary()
    remaining = request_budget

    with ingestion_run(
        SOURCE_CODE,
        "backfill",
        params={
            "targets": [[lid, season] for lid, season in targets],
            "request_budget": request_budget,
            "skip_existing": skip_existing,
        },
        sessionmaker_=sessionmaker_,
    ) as run:
        session = run.session
        source_id = _get_source_id(session)

        for league_id, season in targets:
            state = evaluate_target(session, source_id, league_id, season)
            if skip_existing and state.state in ("complete", "no_data"):
                summary.targets.append(
                    _build_result(session, source_id, league_id, season, state.state, 0)
                )
                continue

            if remaining is not None and remaining <= 0:
                # Tope agotado: el resto queda pendiente para una próxima corrida.
                summary.budget_exhausted = True
                summary.targets.append(
                    _build_result(session, source_id, league_id, season, "pending", 0)
                )
                continue

            child = ingest_league_season(
                adapter,
                league_id,
                season,
                request_budget=remaining,
                skip_existing=skip_existing,
                sessionmaker_=sessionmaker_,
            )
            summary.requests_made += child.requests_made
            if remaining is not None:
                remaining -= child.requests_made

            season_id = _season_id(session, source_id, league_id, season)
            n_matches = _n_matches(session, season_id) if season_id else 0

            if not child.budget_exhausted and n_matches == 0:
                # Profundidad variable: la temporada no trae datos. Registrar y seguir.
                _mark_no_data(session, run.run_id, league_id, season)
                summary.targets.append(
                    TargetResult(
                        league_id, season, "no_data", 0, 0, child.requests_made, str(child.run_id)
                    )
                )
                continue

            # Si las stats se intentaron por completo (no cortadas por budget), marca
            # los terminales que siguen sin stats: la API no las trae.
            if season_id is not None and child.stats_pending == 0 and not child.budget_exhausted:
                _mark_terminal_without_stats(session, run.run_id, season_id)

            final = evaluate_target(session, source_id, league_id, season).state
            summary.targets.append(
                _build_result(
                    session, source_id, league_id, season, final, child.requests_made, child.run_id
                )
            )
            if child.budget_exhausted:
                summary.budget_exhausted = True

        incomplete = any(t.state in ("partial", "pending") for t in summary.targets)
        summary.status = "partial" if incomplete else "success"
        if summary.status == "partial":
            run.mark_partial()
        run.rows_written = sum(t.n_stats_rows for t in summary.targets)
        session.add(
            DataQualityCheck(
                ingestion_run_id=run.run_id,
                check_name="backfill_summary",
                severity="warning" if summary.status == "partial" else "info",
                passed=summary.status != "partial",
                details=summary.as_details(),
            )
        )
        session.flush()

    return summary
