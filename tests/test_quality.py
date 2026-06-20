"""HU 1.6.1 — Tests de los chequeos de calidad (datos sembrados, sin API real)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from valuebet.adapters.sources import seed_sources
from valuebet.db.models.core import (
    Competition,
    Match,
    MatchTeamStats,
    Season,
    SourceEntityMap,
    Sport,
    Team,
)
from valuebet.db.models.meta import DataQualityCheck, Source
from valuebet.db.session import get_session
from valuebet.quality import checks as q

pytestmark = pytest.mark.integration

_TRUNCATE = text(
    "TRUNCATE core.match_team_stats, core.matches, core.source_entity_map, "
    "core.team_aliases, core.team_season, core.player_team, core.players, "
    "core.seasons, core.competitions, core.teams, core.venues, core.countries, "
    "core.sports, raw.payloads, meta.data_quality_checks, meta.ingestion_runs, "
    "meta.sources RESTART IDENTITY CASCADE"
)

_KICKOFF = datetime(2023, 8, 1, 14, 0, tzinfo=UTC)


@pytest.fixture
def seeded(migrated_db):
    with get_session() as session:
        session.execute(_TRUNCATE)
    with get_session() as session:
        seed_sources(session)
    yield


def _base(session) -> tuple[Sport, Season]:
    sport = Sport(code="football", name="Football")
    session.add(sport)
    session.flush()
    comp = Competition(sport_id=sport.id, name="Premier League", kind="league")
    session.add(comp)
    session.flush()
    season = Season(competition_id=comp.id, label="2023")
    session.add(season)
    session.flush()
    return sport, season


def _team(session, sport_id, name) -> Team:
    team = Team(sport_id=sport_id, name=name)
    session.add(team)
    session.flush()
    return team


def _match(
    session, season_id, home, away, *, status="finished", hg=1, ag=0, kickoff=_KICKOFF
) -> Match:
    match = Match(
        season_id=season_id,
        home_team_id=home,
        away_team_id=away,
        status_code=status,
        kickoff_utc=kickoff,
        home_goals=hg,
        away_goals=ag,
    )
    session.add(match)
    session.flush()
    return match


def _stats(session, match_id, team_id, *, is_home=True, possession=0.5, xg=1.0, shots=10) -> None:
    session.add(
        MatchTeamStats(
            match_id=match_id,
            team_id=team_id,
            is_home=is_home,
            possession=possession,
            xg=xg,
            shots=shots,
        )
    )
    session.flush()


def _run_all() -> dict[str, q.CheckResult]:
    with get_session() as session:
        return {c.__name__: c(session) for c in q.ALL_CHECKS}


def test_healthy_base_integrity_checks_pass(seeded) -> None:
    with get_session() as session:
        sport, season = _base(session)
        home = _team(session, sport.id, "Home FC")
        away = _team(session, sport.id, "Away FC")
        match = _match(session, season.id, home.id, away.id, hg=2, ag=1)
        _stats(session, match.id, home.id, is_home=True, possession=0.55, xg=1.8)
        _stats(session, match.id, away.id, is_home=False, possession=0.45, xg=0.9)

    results = _run_all()
    integrity = [
        "check_orphan_match_teams",
        "check_terminal_without_goals",
        "check_goals_without_terminal",
        "check_orphan_stats_match",
        "check_dangling_source_entity_map",
        "check_possession_out_of_range",
        "check_negative_xg",
        "check_negative_goals",
        "check_terminal_without_kickoff",
        "check_stats_pandera",
    ]
    for name in integrity:
        assert results[name].passed, f"{name} debería pasar en base sana"


def test_each_problem_is_detected_with_correct_severity(seeded) -> None:
    with get_session() as session:
        sport, season = _base(session)
        home = _team(session, sport.id, "Home FC")
        away = _team(session, sport.id, "Away FC")
        source_id = session.execute(
            select(Source.id).where(Source.code == "api_sports")
        ).scalar_one()

        # 1) terminal sin goles
        _match(session, season.id, home.id, away.id, hg=None, ag=None)
        # 2) possession 1.5 y xg -1 (en un match con stats)
        m2 = _match(session, season.id, home.id, away.id, hg=1, ag=0)
        _stats(session, m2.id, home.id, possession=1.5, xg=-1.0)
        # 3) terminal sin stats
        _match(session, season.id, home.id, away.id, hg=3, ag=2)
        # 4) source_entity_map con internal_id roto (sin FK, inyectable)
        session.add(
            SourceEntityMap(
                source_id=source_id,
                entity_type="team",
                external_id="ghost",
                internal_id=uuid.uuid4(),
            )
        )
        session.flush()

    results = _run_all()

    def _check(name, severity):
        r = results[name]
        assert r.passed is False, f"{name} debería detectar el problema"
        assert r.severity == severity
        assert r.count >= 1

    _check("check_terminal_without_goals", "error")
    _check("check_possession_out_of_range", "error")
    _check("check_negative_xg", "error")
    _check("check_terminal_without_stats", "warning")
    _check("check_dangling_source_entity_map", "error")
    _check("check_stats_pandera", "error")


def test_completeness_by_season_counts(seeded) -> None:
    with get_session() as session:
        sport, season = _base(session)
        home = _team(session, sport.id, "Home FC")
        away = _team(session, sport.id, "Away FC")
        # 3 partidos terminales; 2 con stats; 1 de ellos con xG.
        a = _match(session, season.id, home.id, away.id, hg=1, ag=0)
        _stats(session, a.id, home.id, xg=1.5)
        b = _match(session, season.id, home.id, away.id, hg=2, ag=2)
        _stats(session, b.id, home.id, xg=None)
        _match(session, season.id, home.id, away.id, hg=0, ag=0)  # sin stats

    with get_session() as session:
        result = q.check_completeness_by_season(session)

    by_season = result.details["by_season"]
    assert len(by_season) == 1
    row = by_season[0]
    assert (row["n_matches"], row["n_terminal"], row["n_with_stats"], row["n_with_xg"]) == (
        3,
        3,
        2,
        1,
    )
    assert result.severity == "info"


def test_pandera_rejects_possession_out_of_range(seeded) -> None:
    with get_session() as session:
        sport, season = _base(session)
        home = _team(session, sport.id, "Home FC")
        away = _team(session, sport.id, "Away FC")
        match = _match(session, season.id, home.id, away.id)
        _stats(session, match.id, home.id, possession=1.5)  # fuera de [0,1]

    with get_session() as session:
        result = q.check_stats_pandera(session)

    assert result.passed is False
    assert result.count >= 1


def test_audit_persists_and_is_read_only(seeded) -> None:
    with get_session() as session:
        sport, season = _base(session)
        home = _team(session, sport.id, "Home FC")
        away = _team(session, sport.id, "Away FC")
        match = _match(session, season.id, home.id, away.id, hg=2, ag=1)
        _stats(session, match.id, home.id, xg=1.0)

    with get_session() as session:
        matches_before = session.scalar(select(func.count()).select_from(Match))
        stats_before = session.scalar(select(func.count()).select_from(MatchTeamStats))

    audit = q.run_quality_audit()
    assert len(audit.results) == len(q.ALL_CHECKS)

    with get_session() as session:
        # Se registraron los chequeos en data_quality_checks bajo la corrida.
        n_rows = session.scalar(
            select(func.count())
            .select_from(DataQualityCheck)
            .where(DataQualityCheck.ingestion_run_id == audit.run_id)
        )
        assert n_rows == len(q.ALL_CHECKS)
        # Solo lectura: core no cambió.
        assert session.scalar(select(func.count()).select_from(Match)) == matches_before
        assert session.scalar(select(func.count()).select_from(MatchTeamStats)) == stats_before
