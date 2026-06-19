"""HU 1.5.2 — Tests del backfill reanudable (adapter falso, sin API real)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from valuebet.adapters.base import RawFetchResult
from valuebet.adapters.sources import seed_sources
from valuebet.db.models.core import Match, MatchTeamStats
from valuebet.db.models.meta import IngestionRun
from valuebet.db.session import get_session
from valuebet.ingestion.backfill import run_backfill

pytestmark = pytest.mark.integration

_TRUNCATE = text(
    "TRUNCATE core.match_team_stats, core.matches, core.source_entity_map, "
    "core.team_aliases, core.team_season, core.player_team, core.players, "
    "core.seasons, core.competitions, core.teams, core.venues, core.countries, "
    "core.sports, raw.payloads, meta.data_quality_checks, meta.ingestion_runs, "
    "meta.sources RESTART IDENTITY CASCADE"
)


@pytest.fixture
def seeded(migrated_db):
    with get_session() as session:
        session.execute(_TRUNCATE)
    with get_session() as session:
        seed_sources(session)
    yield


class FakeBackfillAdapter:
    """Genera payloads canónicos por (liga, temporada) y cuenta peticiones.

    `empty` = conjunto de (league, season) cuyo /fixtures viene vacío (sin datos).
    """

    source_code = "api_sports"

    def __init__(self, *, empty: set[tuple[int, int]] | None = None, n_finished: int = 2) -> None:
        self.empty = set(empty or set())
        self.n_finished = n_finished
        self.requests = 0

    def _teams(self, league: int) -> tuple[int, int]:
        return league * 1000 + 1, league * 1000 + 2

    def fetch_leagues(self, params=None, *, league_id=None) -> RawFetchResult:
        self.requests += 1
        season = (params or {}).get("season") or 2023
        payload = {
            "errors": [],
            "results": 1,
            "response": [
                {
                    "league": {"id": league_id, "name": f"League {league_id}", "type": "League"},
                    "country": {"name": f"Country{league_id}", "code": None},
                    "seasons": [
                        {
                            "year": season,
                            "current": True,
                            "coverage": {
                                "fixtures": {
                                    "events": True,
                                    "lineups": True,
                                    "statistics_fixtures": True,
                                    "statistics_players": True,
                                },
                                "standings": True,
                                "odds": False,
                            },
                        }
                    ],
                }
            ],
        }
        return RawFetchResult.build(
            source_code="api_sports", endpoint="/leagues", payload=payload, params=params
        )

    def fetch_teams(self, league_id, season) -> RawFetchResult:
        self.requests += 1
        t1, t2 = self._teams(league_id)
        payload = {
            "errors": [],
            "results": 2,
            "response": [
                {
                    "team": {
                        "id": t1,
                        "name": f"T{t1}",
                        "code": None,
                        "founded": None,
                        "national": False,
                    },
                    "venue": {
                        "id": league_id * 10 + 1,
                        "name": f"V{league_id}",
                        "city": "X",
                        "capacity": None,
                    },
                },
                {
                    "team": {
                        "id": t2,
                        "name": f"T{t2}",
                        "code": None,
                        "founded": None,
                        "national": False,
                    },
                    "venue": {"id": None, "name": None, "city": None, "capacity": None},
                },
            ],
        }
        return RawFetchResult.build(
            source_code="api_sports",
            endpoint="/teams",
            payload=payload,
            params={"league": league_id, "season": season},
        )

    def fetch_fixtures(self, league_id, season) -> RawFetchResult:
        self.requests += 1
        if (league_id, season) in self.empty:
            response = []
        else:
            t1, t2 = self._teams(league_id)
            response = [
                {
                    "fixture": {
                        "id": league_id * 100000 + season * 10 + k,
                        "timezone": "UTC",
                        "timestamp": 1690000000 + k * 86400,
                        "status": {"short": "FT"},
                        "venue": {"id": league_id * 10 + 1},
                    },
                    "league": {"id": league_id, "season": season, "round": f"R{k}"},
                    "teams": {"home": {"id": t1}, "away": {"id": t2}},
                    "goals": {"home": 1, "away": 0},
                    "score": {"halftime": {"home": 0, "away": 0}},
                }
                for k in range(self.n_finished)
            ]
        payload = {"errors": [], "results": len(response), "response": response}
        return RawFetchResult.build(
            source_code="api_sports",
            endpoint="/fixtures",
            payload=payload,
            params={"league": league_id, "season": season},
        )

    def fetch_fixture_statistics(self, fixture_id) -> RawFetchResult:
        self.requests += 1
        league = fixture_id // 100000
        t1, t2 = self._teams(league)

        def team_stats(tid):
            return {
                "team": {"id": tid},
                "statistics": [
                    {"type": "Total Shots", "value": 10},
                    {"type": "Ball Possession", "value": "50%"},
                    {"type": "expected_goals", "value": "1.2"},
                ],
            }

        payload = {
            "errors": [],
            "results": 2,
            "parameters": {"fixture": fixture_id},
            "response": [team_stats(t1), team_stats(t2)],
        }
        return RawFetchResult.build(
            source_code="api_sports",
            endpoint="/fixtures/statistics",
            payload=payload,
            params={"fixture": fixture_id},
        )


def _state(summary, league, season) -> str:
    return next(t.state for t in summary.targets if (t.league_id, t.season) == (league, season))


def test_full_backfill_completes_all(seeded) -> None:
    adapter = FakeBackfillAdapter()
    summary = run_backfill(adapter, [(39, 2023), (140, 2023)])

    assert summary.status == "success"
    assert all(t.state == "complete" for t in summary.targets)
    with get_session() as session:
        # 2 ligas * 2 partidos = 4 matches; 4 * 2 equipos = 8 filas de stats.
        assert session.scalar(select(func.count()).select_from(Match)) == 4
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == 8
        # Una sola run padre 'backfill'.
        assert (
            session.scalar(
                select(func.count())
                .select_from(IngestionRun)
                .where(IngestionRun.flow_name == "backfill")
            )
            == 1
        )


def test_resumable_with_low_budget_then_completes(seeded) -> None:
    targets = [(39, 2023), (140, 2023)]
    # Cada league-season fresca cuesta 5 (leagues+teams+fixtures+2 stats).
    first = run_backfill(FakeBackfillAdapter(), targets, request_budget=7)
    assert first.status == "partial"
    assert first.requests_made <= 7  # tope nunca excedido
    assert _state(first, 39, 2023) == "complete"
    assert _state(first, 140, 2023) in ("partial", "pending")

    # Segunda corrida: continúa lo que faltaba, sin re-fetchear lo hecho.
    second = run_backfill(FakeBackfillAdapter(), targets)
    assert second.status == "success"
    assert all(t.state == "complete" for t in second.targets)
    assert second.requests_made < first.requests_made
    with get_session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 4
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == 8


def test_third_run_idempotent_zero_requests(seeded) -> None:
    targets = [(39, 2023), (140, 2023)]
    run_backfill(FakeBackfillAdapter(), targets)
    run_backfill(FakeBackfillAdapter(), targets)
    with get_session() as session:
        before = session.scalar(select(func.count()).select_from(MatchTeamStats))

    third = run_backfill(FakeBackfillAdapter(), targets)
    with get_session() as session:
        after = session.scalar(select(func.count()).select_from(MatchTeamStats))

    assert third.requests_made == 0  # todo completo → 0 peticiones
    assert all(t.state == "complete" for t in third.targets)
    assert before == after == 8


def test_budget_never_exceeded(seeded) -> None:
    for budget in (1, 2, 3, 5, 6):
        with get_session() as session:
            session.execute(_TRUNCATE)
        with get_session() as session:
            seed_sources(session)
        summary = run_backfill(
            FakeBackfillAdapter(), [(39, 2023), (140, 2023)], request_budget=budget
        )
        assert summary.requests_made <= budget


def test_variable_depth_empty_season_recorded_not_fatal(seeded) -> None:
    # (39, 2099) no tiene datos: /fixtures vacío.
    adapter = FakeBackfillAdapter(empty={(39, 2099)})
    summary = run_backfill(adapter, [(39, 2023), (39, 2099)])

    assert summary.status == "success"  # sin datos no es error
    assert _state(summary, 39, 2023) == "complete"
    assert _state(summary, 39, 2099) == "no_data"

    # Una segunda corrida salta la temporada sin datos (0 peticiones para ella).
    adapter2 = FakeBackfillAdapter(empty={(39, 2099)})
    second = run_backfill(adapter2, [(39, 2023), (39, 2099)])
    no_data_target = next(t for t in second.targets if (t.league_id, t.season) == (39, 2099))
    assert no_data_target.state == "no_data"
    assert no_data_target.requests == 0


def test_completeness_partial_when_stats_missing(seeded) -> None:
    # Budget que cubre catálogo+fixtures de (39,2023) pero no todas sus stats.
    # leagues(1)+teams(1)+fixtures(1)=3, +1 stat = 4 → queda 1 partido sin stats.
    summary = run_backfill(FakeBackfillAdapter(), [(39, 2023)], request_budget=4)
    assert _state(summary, 39, 2023) == "partial"
    with get_session() as session:
        # 2 partidos, pero solo 1 con stats (2 filas).
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == 2
