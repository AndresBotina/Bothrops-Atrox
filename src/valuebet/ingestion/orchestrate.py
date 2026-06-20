"""Flujo de ingesta encadenado para una (liga, temporada) — HU 1.5.1.

Orquesta, en el ORDEN DE DEPENDENCIAS obligatorio, lo ya construido:
  1. Catálogo: /leagues + /teams → normalize catalog (competiciones, temporadas,
     equipos, estadios).
  2. Partidos: /fixtures → normalize fixtures (core.matches).
  3. Stats: por cada partido TERMINAL ya en core, /fixtures/statistics → normalize
     stats (core.match_team_stats).

Garantías:
  * UNA sola corrida padre en `meta.ingestion_runs`; TODOS los payloads de los
    sub-fetches enlazan a ella (auditable por (liga, temporada)).
  * El orden evita `missing_refs`: equipos antes que partidos, partidos antes que stats.
  * `request_budget`: tope de peticiones; al alcanzarlo, cierra en 'partial' (no falla)
    y deja registrado lo pendiente — semilla de la reanudabilidad (1.5.2).
  * `skip_existing`: evita refetch (catálogo ya en core, partido ya con stats) para no
    quemar cuota. La corrección la garantiza la idempotencia; esto sólo ahorra.
  * Resiliencia: si el fetch de stats de UN partido falla, se registra y se sigue; la
    run queda 'partial'.

NO reescribe la lógica de normalización: reutiliza las funciones de catalog/fixtures/
stats. NO toca `raw` salvo para INSERTAR los payloads de los fetches (append-only).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from valuebet.adapters.api_football import ApiFootballAdapter
from valuebet.adapters.http import QuotaExceededError
from valuebet.db.models.core import Match, MatchTeamStats, SourceEntityMap
from valuebet.db.models.meta import DataQualityCheck
from valuebet.ingestion.normalize_catalog import (
    SOURCE_CODE,
    NormalizeStats,
    _ensure_sport,
    _get_source_id,
    _normalize_leagues,
    _normalize_teams,
    _resolve_via_map,
)
from valuebet.ingestion.normalize_fixtures import (
    RESULT_BEARING_STATUSES,
    FixtureStats,
    _normalize_fixtures_payload,
)
from valuebet.ingestion.normalize_stats import StatsResult, _normalize_stats_payload
from valuebet.ingestion.raw import RunHandle, ingestion_run


class _Budget:
    """Contador de peticiones con tope opcional."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.used = 0

    def available(self) -> bool:
        return self.limit is None or self.used < self.limit

    def spend(self) -> None:
        self.used += 1


@dataclass
class IngestSummary:
    """Resumen auditable de una corrida de ingesta encadenada."""

    league_id: int
    season: int
    run_id: uuid.UUID | None = None  # id de la corrida hija (trazabilidad backfill)
    catalog: NormalizeStats = field(default_factory=NormalizeStats)
    catalog_skipped: bool = False
    fixtures: FixtureStats = field(default_factory=FixtureStats)
    stats: StatsResult = field(default_factory=StatsResult)
    stats_skipped_existing: int = 0
    stats_pending: int = 0
    stats_failed: int = 0
    requests_made: int = 0
    budget_exhausted: bool = False
    quota_exceeded: bool = False
    issues: list[dict] = field(default_factory=list)
    status: str = "success"

    def is_partial(self) -> bool:
        return (
            self.budget_exhausted
            or self.quota_exceeded
            or self.stats_pending > 0
            or self.stats_failed > 0
        )

    def as_details(self) -> dict:
        return {
            "league": self.league_id,
            "season": self.season,
            "run_id": str(self.run_id) if self.run_id else None,
            "catalog": self.catalog.as_details(),
            "catalog_skipped": self.catalog_skipped,
            "fixtures": self.fixtures.as_details(),
            "stats": self.stats.as_details(),
            "stats_skipped_existing": self.stats_skipped_existing,
            "stats_pending": self.stats_pending,
            "stats_failed": self.stats_failed,
            "requests_made": self.requests_made,
            "budget_exhausted": self.budget_exhausted,
            "quota_exceeded": self.quota_exceeded,
            "status": self.status,
            "issues": self.issues,
        }


def _terminal_matches(session: Session, season_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """(match_id, fixture_external_id) de los partidos terminales de la temporada."""
    rows = session.execute(
        select(Match.id, SourceEntityMap.external_id)
        .join(
            SourceEntityMap,
            (SourceEntityMap.internal_id == Match.id) & (SourceEntityMap.entity_type == "match"),
        )
        .where(
            Match.season_id == season_id,
            Match.status_code.in_(RESULT_BEARING_STATUSES),
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


def _has_stats(session: Session, match_id: uuid.UUID) -> bool:
    count = session.scalar(
        select(func.count()).select_from(MatchTeamStats).where(MatchTeamStats.match_id == match_id)
    )
    return bool(count)


def _ingest_catalog(
    adapter: ApiFootballAdapter,
    run: RunHandle,
    session: Session,
    source_id: uuid.UUID,
    sport_id: uuid.UUID,
    league_id: int,
    season: int,
    budget: _Budget,
    summary: IngestSummary,
) -> None:
    if not budget.available():
        summary.budget_exhausted = True
        return
    budget.spend()
    # /leagues filtra una liga concreta con 'id' (lo mapea fetch_leagues), NO 'league'.
    leagues_res = adapter.fetch_leagues({"season": season}, league_id=league_id)
    run.persist(leagues_res)
    _normalize_leagues(session, source_id, sport_id, leagues_res.payload, summary.catalog)

    if not budget.available():
        summary.budget_exhausted = True
        return
    budget.spend()
    teams_res = adapter.fetch_teams(league_id, season)
    run.persist(teams_res)
    _normalize_teams(session, source_id, sport_id, teams_res.payload, summary.catalog)


def _ingest_stats(
    adapter: ApiFootballAdapter,
    run: RunHandle,
    session: Session,
    source_id: uuid.UUID,
    season_id: uuid.UUID,
    budget: _Budget,
    skip_existing: bool,
    summary: IngestSummary,
) -> None:
    worklist: list[tuple[uuid.UUID, str]] = []
    for match_id, fixture_ext in _terminal_matches(session, season_id):
        if skip_existing and _has_stats(session, match_id):
            summary.stats_skipped_existing += 1
        else:
            worklist.append((match_id, fixture_ext))

    for _match_id, fixture_ext in worklist:
        if not budget.available():
            summary.stats_pending += 1
            summary.budget_exhausted = True
            continue
        budget.spend()
        try:
            stats_res = adapter.fetch_fixture_statistics(int(fixture_ext))
        except QuotaExceededError:
            # Cuota agotada: NO es "un partido que falla"; corta el flujo (parada limpia).
            raise
        except Exception as exc:  # noqa: BLE001 — un partido no debe tumbar el flujo
            summary.stats_failed += 1
            summary.issues.append({"fixture_id": fixture_ext, "stage": "fetch", "error": str(exc)})
            continue
        try:
            run.persist(stats_res)
            _normalize_stats_payload(session, source_id, stats_res.payload, summary.stats)
        except Exception as exc:  # noqa: BLE001
            summary.stats_failed += 1
            summary.issues.append(
                {"fixture_id": fixture_ext, "stage": "normalize", "error": str(exc)}
            )


def _record_summary(session: Session, run_id: uuid.UUID, summary: IngestSummary) -> None:
    session.add(
        DataQualityCheck(
            ingestion_run_id=run_id,
            check_name="ingest_league_season",
            severity="warning" if summary.is_partial() else "info",
            passed=not summary.is_partial(),
            details=summary.as_details(),
        )
    )
    session.flush()


def ingest_league_season(
    adapter: ApiFootballAdapter,
    league_id: int,
    season: int,
    *,
    request_budget: int | None = None,
    skip_existing: bool = True,
    sessionmaker_=None,
) -> IngestSummary:
    """Ejecuta la cadena fetch→raw→normalize para una (liga, temporada).

    Devuelve un `IngestSummary` con los conteos y el estado final de la corrida.
    """
    summary = IngestSummary(league_id=league_id, season=season)
    budget = _Budget(request_budget)
    season_ext = f"{league_id}:{season}"

    with ingestion_run(
        SOURCE_CODE,
        "ingest_league_season",
        params={
            "league": league_id,
            "season": season,
            "request_budget": request_budget,
            "skip_existing": skip_existing,
        },
        sessionmaker_=sessionmaker_,
    ) as run:
        session = run.session
        source_id = _get_source_id(session)
        sport_id = _ensure_sport(session)

        try:
            # 1. CATÁLOGO (equipos+estadios antes que los partidos).
            if (
                skip_existing
                and _resolve_via_map(session, source_id, "season", season_ext) is not None
            ):
                summary.catalog_skipped = True
            else:
                _ingest_catalog(
                    adapter, run, session, source_id, sport_id, league_id, season, budget, summary
                )

            # 2. PARTIDOS (antes que las stats).
            if budget.available():
                budget.spend()
                fixtures_res = adapter.fetch_fixtures(league_id, season)
                run.persist(fixtures_res)
                _normalize_fixtures_payload(
                    session, source_id, fixtures_res.payload, summary.fixtures
                )
            else:
                summary.budget_exhausted = True

            # 3. STATS por partido terminal.
            season_id = _resolve_via_map(session, source_id, "season", season_ext)
            if season_id is not None:
                _ingest_stats(
                    adapter, run, session, source_id, season_id, budget, skip_existing, summary
                )
        except QuotaExceededError as exc:
            # Cuota de la API agotada: parada LIMPIA (no fatal). Se preserva lo ingerido
            # y la corrida cierra 'partial'; reanudable tras el reset diario.
            summary.quota_exceeded = True
            summary.budget_exhausted = True
            summary.issues.append({"stage": "quota", "error": str(exc)})

        summary.run_id = run.run_id
        summary.requests_made = budget.used
        summary.status = "partial" if summary.is_partial() else "success"
        if summary.status == "partial":
            run.mark_partial()
        run.rows_written = (
            summary.catalog.created
            + summary.fixtures.created
            + summary.fixtures.updated
            + summary.stats.rows_upserted
        )
        _record_summary(session, run.run_id, summary)

    return summary
