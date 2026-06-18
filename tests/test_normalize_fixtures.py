"""HU 1.3.3 — Tests de normalización de partidos raw -> core.matches (sin API real)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from valuebet.adapters.sources import seed_sources
from valuebet.db.models.core import Match, SourceEntityMap, Team
from valuebet.db.models.meta import DataQualityCheck, Source
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.normalize_catalog import normalize_catalog
from valuebet.ingestion.normalize_fixtures import normalize_fixtures

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


def _match_by_fixture(session, fixture_id: int) -> Match | None:
    internal = session.execute(
        select(SourceEntityMap.internal_id).where(
            SourceEntityMap.entity_type == "match",
            SourceEntityMap.external_id == str(fixture_id),
        )
    ).scalar_one_or_none()
    return session.get(Match, internal) if internal is not None else None


def _latest_fixture_dq(session) -> DataQualityCheck:
    return (
        session.execute(
            select(DataQualityCheck)
            .where(DataQualityCheck.check_name == "fixture_normalization")
            .order_by(DataQualityCheck.checked_at.desc())
        )
        .scalars()
        .first()
    )


@pytest.fixture
def catalog(migrated_db):
    """Fuentes + catálogo normalizado (prerequisito de los partidos)."""
    with get_session() as session:
        seed_sources(session)
    _seed_raw("/leagues", _load("leagues.json"))
    _seed_raw("/teams", _load("teams.json"))
    normalize_catalog()
    yield


def test_not_started_fixture_is_scheduled_without_goals(catalog) -> None:
    _seed_raw("/fixtures", _load("fixtures.json"))
    normalize_fixtures()

    with get_session() as session:
        match = _match_by_fixture(session, 1001)
        assert match is not None
        assert match.status_code == "scheduled"
        assert match.home_goals is None
        assert match.away_goals is None
        # kickoff SIEMPRE en UTC.
        assert match.kickoff_utc.utcoffset() == timedelta(0)
        assert match.kickoff_tz == "UTC"


def test_finished_fixture_has_status_and_goals(catalog) -> None:
    _seed_raw("/fixtures", _load("fixtures.json"))
    normalize_fixtures()

    with get_session() as session:
        match = _match_by_fixture(session, 1002)
        assert match is not None
        assert match.status_code == "finished"
        assert match.home_goals == 2
        assert match.away_goals == 1
        assert match.home_goals_ht == 1
        assert match.away_goals_ht == 0


def test_state_change_updates_in_place(catalog) -> None:
    # Primero NS (sin jugar).
    _seed_raw("/fixtures", _load("fixtures_update_ns.json"))
    normalize_fixtures()
    with get_session() as session:
        match = _match_by_fixture(session, 2001)
        first_id = match.id
        assert match.status_code == "scheduled"
        assert match.home_goals is None

    # Luego FT con resultado: mismo fixture.
    _seed_raw("/fixtures", _load("fixtures_update_ft.json"))
    stats = normalize_fixtures()
    with get_session() as session:
        match = _match_by_fixture(session, 2001)
        assert match.id == first_id  # MISMA fila (UPSERT, no duplica)
        assert match.status_code == "finished"
        assert match.home_goals == 3
        assert match.away_goals == 2
        # un solo mapeo para el fixture 2001
        n_map = session.scalar(
            select(func.count())
            .select_from(SourceEntityMap)
            .where(SourceEntityMap.entity_type == "match", SourceEntityMap.external_id == "2001")
        )
        assert n_map == 1
    assert stats.updated >= 1
    assert stats.created == 0


def test_normalization_is_idempotent(catalog) -> None:
    _seed_raw("/fixtures", _load("fixtures.json"))
    normalize_fixtures()

    def _counts(session) -> tuple[int, int]:
        matches = session.scalar(select(func.count()).select_from(Match))
        match_maps = session.scalar(
            select(func.count())
            .select_from(SourceEntityMap)
            .where(SourceEntityMap.entity_type == "match")
        )
        return matches, match_maps

    with get_session() as session:
        before = _counts(session)
    stats2 = normalize_fixtures()
    with get_session() as session:
        after = _counts(session)

    assert before == after
    assert stats2.created == 0


def test_unknown_status_is_recorded_not_forced(catalog) -> None:
    _seed_raw("/fixtures", _load("fixtures.json"))
    stats = normalize_fixtures()

    with get_session() as session:
        # El partido con status desconocido NO se materializa.
        assert _match_by_fixture(session, 1003) is None
        dq = _latest_fixture_dq(session)
        assert dq.passed is False
        assert dq.severity == "warning"
        reasons = {issue["reason"] for issue in dq.details["issues"]}
        assert "unknown_status" in reasons

    assert any(i["fixture_id"] == 1003 for i in stats.issues)


def test_missing_team_does_not_create_phantom(catalog) -> None:
    teams_before = None
    with get_session() as session:
        teams_before = session.scalar(select(func.count()).select_from(Team))

    _seed_raw("/fixtures", _load("fixtures.json"))
    normalize_fixtures()

    with get_session() as session:
        # No se creó equipo fantasma para el id 55555.
        assert (
            session.execute(
                select(SourceEntityMap.internal_id).where(
                    SourceEntityMap.entity_type == "team",
                    SourceEntityMap.external_id == "55555",
                )
            ).scalar_one_or_none()
            is None
        )
        assert session.scalar(select(func.count()).select_from(Team)) == teams_before
        # El partido no-normalizable no está en core.matches.
        assert _match_by_fixture(session, 1004) is None
        dq = _latest_fixture_dq(session)
        reasons = {issue["reason"] for issue in dq.details["issues"]}
        assert "missing_refs" in reasons
