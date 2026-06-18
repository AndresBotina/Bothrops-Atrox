"""HU 1.3.4 — Tests de normalización de stats post-partido (sin API real)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from valuebet.adapters.sources import seed_sources
from valuebet.db.models.core import Match, MatchTeamStats, SourceEntityMap
from valuebet.db.models.meta import DataQualityCheck, Source
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.normalize_catalog import normalize_catalog
from valuebet.ingestion.normalize_fixtures import normalize_fixtures
from valuebet.ingestion.normalize_stats import normalize_stats

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures" / "api_football"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _seed_raw(endpoint: str, payload: dict) -> None:
    with get_session() as session:
        source_id = session.execute(
            select(Source.id).where(Source.code == "api_sports")
        ).scalar_one()
        session.add(
            Payload(
                source_id=source_id,
                endpoint=endpoint,
                request_hash="test-hash",
                http_status=200,
                payload=payload,
            )
        )


def _match_id(session, fixture_id: int) -> object:
    return session.execute(
        select(SourceEntityMap.internal_id).where(
            SourceEntityMap.entity_type == "match",
            SourceEntityMap.external_id == str(fixture_id),
        )
    ).scalar_one_or_none()


def _stats_rows(session, fixture_id: int) -> list[MatchTeamStats]:
    match_id = _match_id(session, fixture_id)
    if match_id is None:
        return []
    return list(
        session.execute(select(MatchTeamStats).where(MatchTeamStats.match_id == match_id)).scalars()
    )


def _latest_stats_dq(session) -> DataQualityCheck:
    return (
        session.execute(
            select(DataQualityCheck)
            .where(DataQualityCheck.check_name == "stats_normalization")
            .order_by(DataQualityCheck.checked_at.desc())
        )
        .scalars()
        .first()
    )


@pytest.fixture
def prepared(migrated_db):
    """Catálogo + partidos normalizados (match 1002 finished) y stats table limpia."""
    with get_session() as session:
        seed_sources(session)
    _seed_raw("/leagues", _load("leagues.json"))
    _seed_raw("/teams", _load("teams.json"))
    _seed_raw("/fixtures", _load("fixtures.json"))
    normalize_catalog()
    normalize_fixtures()
    with get_session() as session:
        session.execute(delete(MatchTeamStats))
    yield


def test_full_stats_two_rows_with_is_home(prepared) -> None:
    _seed_raw("/fixtures/statistics", _load("statistics_full.json"))
    result = normalize_stats()
    assert result.rows_upserted == 2

    with get_session() as session:
        rows = {r.team_id: r for r in _stats_rows(session, 1002)}
        assert len(rows) == 2

        match = session.get(Match, _match_id(session, 1002))
        home = rows[match.home_team_id]  # equipo 999
        away = rows[match.away_team_id]  # equipo 33
        assert home.is_home is True
        assert away.is_home is False
        # posesión normalizada a fracción 0..1
        assert home.possession == pytest.approx(0.55)
        assert away.possession == pytest.approx(0.45)
        assert home.shots == 14
        assert home.shots_on_tgt == 6


def test_possession_and_xg_parsing(prepared) -> None:
    _seed_raw("/fixtures/statistics", _load("statistics_full.json"))
    normalize_stats()

    with get_session() as session:
        rows = {r.team_id: r for r in _stats_rows(session, 1002)}
        match = session.get(Match, _match_id(session, 1002))
        home = rows[match.home_team_id]
        away = rows[match.away_team_id]
        assert (home.possession, away.possession) == pytest.approx((0.55, 0.45))
        # xG presente -> float
        assert home.xg == pytest.approx(1.8)
        assert away.xg == pytest.approx(0.9)


def test_absent_stats_recorded_no_rows_no_error(prepared) -> None:
    _seed_raw("/fixtures/statistics", _load("statistics_empty.json"))
    result = normalize_stats()

    assert result.rows_upserted == 0
    assert result.matches_without_stats == 1
    with get_session() as session:
        assert _stats_rows(session, 1002) == []
        dq = _latest_stats_dq(session)
        reasons = {issue["reason"] for issue in dq.details["issues"]}
        assert "stats_ausentes" in reasons


def test_missing_xg_is_none_not_zero(prepared) -> None:
    _seed_raw("/fixtures/statistics", _load("statistics_no_xg.json"))
    normalize_stats()

    with get_session() as session:
        rows = _stats_rows(session, 1002)
        assert len(rows) == 2
        for row in rows:
            assert row.xg is None  # ausencia != 0.0
            assert row.shots is not None  # el resto sí se pobló
            assert row.possession is not None


def test_idempotent_upsert(prepared) -> None:
    _seed_raw("/fixtures/statistics", _load("statistics_full.json"))
    normalize_stats()
    with get_session() as session:
        before = session.scalar(select(func.count()).select_from(MatchTeamStats))

    result2 = normalize_stats()
    with get_session() as session:
        after = session.scalar(select(func.count()).select_from(MatchTeamStats))

    assert before == after == 2
    assert result2.rows_upserted == 2  # UPSERT re-aplica, no duplica


def test_missing_match_does_not_create_anything(prepared) -> None:
    matches_before = None
    with get_session() as session:
        matches_before = session.scalar(select(func.count()).select_from(Match))

    _seed_raw("/fixtures/statistics", _load("statistics_missing_match.json"))
    result = normalize_stats()

    assert result.rows_upserted == 0
    assert result.skipped >= 1
    with get_session() as session:
        # no se creó match fantasma 9999
        assert _match_id(session, 9999) is None
        assert session.scalar(select(func.count()).select_from(Match)) == matches_before
        dq = _latest_stats_dq(session)
        reasons = {issue["reason"] for issue in dq.details["issues"]}
        assert "missing_refs" in reasons
