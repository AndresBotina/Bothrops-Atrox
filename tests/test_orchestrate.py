"""HU 1.5.1 — Tests del flujo de ingesta encadenado (adapter falso, sin API real)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text

from valuebet.adapters.api_football import BASE_URL, ApiFootballAdapter
from valuebet.adapters.base import RawFetchResult
from valuebet.adapters.http import HTTPClient
from valuebet.adapters.sources import seed_sources
from valuebet.db.models.core import Country, Match, MatchTeamStats, SourceEntityMap, Team
from valuebet.db.models.meta import IngestionRun
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.orchestrate import ingest_league_season

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures" / "api_football"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeApiFootball:
    """Adapter falso: devuelve payloads canónicos por endpoint y cuenta llamadas."""

    source_code = "api_sports"

    def __init__(self, *, fail_fixture: int | None = None) -> None:
        self.fail_fixture = fail_fixture
        self.calls: list[str] = []
        self._stats_template = _load("statistics_full.json")

    def fetch_leagues(self, params=None, *, league_id=None) -> RawFetchResult:
        self.calls.append("leagues")
        query = dict(params or {})
        if league_id is not None:
            query["id"] = int(league_id)
        return RawFetchResult.build(
            source_code="api_sports",
            endpoint="/leagues",
            payload=_load("leagues.json"),
            params=query,
        )

    def fetch_teams(self, league_id: int, season: int) -> RawFetchResult:
        self.calls.append("teams")
        return RawFetchResult.build(
            source_code="api_sports",
            endpoint="/teams",
            payload=_load("teams.json"),
            params={"league": league_id, "season": season},
        )

    def fetch_fixtures(self, league_id: int, season: int) -> RawFetchResult:
        self.calls.append("fixtures")
        return RawFetchResult.build(
            source_code="api_sports",
            endpoint="/fixtures",
            payload=_load("fixtures_orch.json"),
            params={"league": league_id, "season": season},
        )

    def fetch_fixture_statistics(self, fixture_id: int) -> RawFetchResult:
        self.calls.append(f"stats:{fixture_id}")
        if self.fail_fixture == fixture_id:
            raise RuntimeError("fallo transitorio agotado")
        payload = copy.deepcopy(self._stats_template)
        payload["parameters"] = {"fixture": fixture_id}
        return RawFetchResult.build(
            source_code="api_sports",
            endpoint="/fixtures/statistics",
            payload=payload,
            params={"fixture": fixture_id},
        )


# Tablas de entidad a vaciar entre tests (NO los lookups sembrados por la migración:
# core.match_statuses / market.market_types se conservan).
_TRUNCATE = text(
    "TRUNCATE core.match_team_stats, core.matches, core.source_entity_map, "
    "core.team_aliases, core.team_season, core.player_team, core.players, "
    "core.seasons, core.competitions, core.teams, core.venues, core.countries, "
    "core.sports, raw.payloads, meta.data_quality_checks, meta.ingestion_runs, "
    "meta.sources RESTART IDENTITY CASCADE"
)


@pytest.fixture
def seeded(migrated_db):
    """Base limpia (entidades truncadas) + fuentes sembradas, para aislar cada test."""
    with get_session() as session:
        session.execute(_TRUNCATE)
    with get_session() as session:
        seed_sources(session)
    yield


def _run_row(session, run_id) -> IngestionRun:
    return session.get(IngestionRun, run_id)


def _latest_run_id(session) -> object:
    return session.execute(
        select(IngestionRun.id)
        .where(IngestionRun.flow_name == "ingest_league_season")
        .order_by(IngestionRun.started_at.desc())
    ).scalar()


def test_full_flow_success_with_linked_payloads(seeded) -> None:
    summary = ingest_league_season(FakeApiFootball(), 39, 2023)

    assert summary.status == "success"
    assert summary.fixtures.created == 4  # 1 NS + 3 FT
    assert summary.stats.rows_upserted == 6  # 3 partidos terminales * 2 equipos

    with get_session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 4
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == 6
        assert session.scalar(select(func.count()).select_from(Country)) == 1
        assert session.scalar(select(func.count()).select_from(Team)) == 2

        # Todos los payloads enlazados a UNA run padre.
        run_id = _latest_run_id(session)
        assert _run_row(session, run_id).status == "success"
        n_payloads = session.scalar(
            select(func.count()).select_from(Payload).where(Payload.ingestion_run_id == run_id)
        )
        assert n_payloads == 6  # leagues + teams + fixtures + 3 stats


def test_order_avoids_missing_refs(seeded) -> None:
    summary = ingest_league_season(FakeApiFootball(), 39, 2023)

    # El orden (catálogo→partidos→stats) evita missing_refs.
    fixture_reasons = {i["reason"] for i in summary.fixtures.issues}
    assert "missing_refs" not in fixture_reasons
    stats_reasons = {i["reason"] for i in summary.stats.issues}
    assert "missing_refs" not in stats_reasons
    assert summary.stats_skipped_existing == 0


def test_idempotent_and_skip_existing_saves_requests(seeded) -> None:
    first = ingest_league_season(FakeApiFootball(), 39, 2023)
    with get_session() as session:
        matches_1 = session.scalar(select(func.count()).select_from(Match))
        stats_1 = session.scalar(select(func.count()).select_from(MatchTeamStats))

    second = ingest_league_season(FakeApiFootball(), 39, 2023)
    with get_session() as session:
        matches_2 = session.scalar(select(func.count()).select_from(Match))
        stats_2 = session.scalar(select(func.count()).select_from(MatchTeamStats))

    assert (matches_1, stats_1) == (matches_2, stats_2) == (4, 6)  # sin duplicados
    assert second.requests_made < first.requests_made  # skip_existing ahorra cuota
    assert second.catalog_skipped is True
    assert second.stats_skipped_existing == 3


def test_request_budget_stops_partial_then_completes(seeded) -> None:
    # budget=4: leagues+teams+fixtures+1 stat; 2 stats quedan pendientes.
    partial = ingest_league_season(FakeApiFootball(), 39, 2023, request_budget=4)
    assert partial.status == "partial"
    assert partial.stats_pending == 2
    assert partial.requests_made == 4
    with get_session() as session:
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == 2
        run_id = _latest_run_id(session)
        assert _run_row(session, run_id).status == "partial"

    # Corrida posterior sin budget completa lo pendiente.
    done = ingest_league_season(FakeApiFootball(), 39, 2023)
    assert done.status == "success"
    with get_session() as session:
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == 6


def test_orchestrator_arms_catalog_requests_correctly(seeded) -> None:
    """Cadena completa a través de httpx: cada endpoint usa SU parámetro de liga.

    Regresión del bug 1.5.1: /leagues debe ir con 'id' (no 'league').
    """
    requests: list[httpx.Request] = []
    stats_template = _load("statistics_full.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/leagues":
            body = _load("leagues.json")
        elif path == "/teams":
            body = _load("teams.json")
        elif path == "/fixtures":
            body = _load("fixtures_orch.json")
        elif path == "/fixtures/statistics":
            body = copy.deepcopy(stats_template)
            body["parameters"] = {"fixture": request.url.params.get("fixture")}
        else:  # pragma: no cover - ruta inesperada
            body = {"errors": [], "results": 0, "response": []}
        return httpx.Response(200, json=body)

    inner = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    adapter = ApiFootballAdapter(HTTPClient(client=inner, max_attempts=2, backoff_base=0.0))

    summary = ingest_league_season(adapter, 39, 2023)
    assert summary.status == "success"

    by_path: dict[str, httpx.Request] = {r.url.path: r for r in requests}
    # /leagues -> 'id' (NO 'league')
    assert by_path["/leagues"].url.params.get("id") == "39"
    assert "league" not in by_path["/leagues"].url.params
    # /teams y /fixtures -> 'league'
    assert by_path["/teams"].url.params.get("league") == "39"
    assert by_path["/fixtures"].url.params.get("league") == "39"


def test_failing_stats_fetch_does_not_break_flow(seeded) -> None:
    summary = ingest_league_season(FakeApiFootball(fail_fixture=1005), 39, 2023)

    assert summary.status == "partial"
    assert summary.stats_failed == 1
    with get_session() as session:
        # 1002 y 1006 sí tienen stats (4 filas); 1005 no.
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == 4
        failed_match = session.execute(
            select(SourceEntityMap.internal_id).where(
                SourceEntityMap.entity_type == "match", SourceEntityMap.external_id == "1005"
            )
        ).scalar_one()
        n = session.scalar(
            select(func.count())
            .select_from(MatchTeamStats)
            .where(MatchTeamStats.match_id == failed_match)
        )
        assert n == 0
