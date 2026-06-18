"""HU 1.3.2 — Tests de normalización del catálogo raw -> core (sin API real)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from valuebet.adapters.sources import seed_sources
from valuebet.db.models.core import (
    Competition,
    Country,
    Season,
    SourceEntityMap,
    Team,
    Venue,
)
from valuebet.db.models.meta import Source
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.normalize_catalog import normalize_catalog

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


def _counts(session) -> dict[str, int]:
    return {
        "countries": session.scalar(select(func.count()).select_from(Country)),
        "competitions": session.scalar(select(func.count()).select_from(Competition)),
        "seasons": session.scalar(select(func.count()).select_from(Season)),
        "teams": session.scalar(select(func.count()).select_from(Team)),
        "venues": session.scalar(select(func.count()).select_from(Venue)),
        "map": session.scalar(select(func.count()).select_from(SourceEntityMap)),
    }


@pytest.fixture
def seeded(migrated_db):
    with get_session() as session:
        seed_sources(session)
    yield


def test_normalize_creates_core_entities_and_map(seeded) -> None:
    _seed_raw("/leagues", _load("leagues.json"))
    _seed_raw("/teams", _load("teams.json"))

    stats = normalize_catalog()
    assert stats.created > 0

    with get_session() as session:
        counts = _counts(session)
        assert counts["countries"] == 1
        assert counts["competitions"] == 1
        assert counts["seasons"] == 1
        assert counts["teams"] == 2
        assert counts["venues"] == 1  # el venue null de Mystery FC se omite
        # map: competition(1) + season(1) + team(2) + venue(1) = 5 (countries NO se mapean)
        assert counts["map"] == 5

        comp = session.execute(select(Competition)).scalars().one()
        assert comp.name == "Premier League"
        assert comp.kind == "league"


def test_normalization_is_idempotent(seeded) -> None:
    _seed_raw("/leagues", _load("leagues.json"))
    _seed_raw("/teams", _load("teams.json"))

    normalize_catalog()
    with get_session() as session:
        before = _counts(session)

    stats2 = normalize_catalog()
    with get_session() as session:
        after = _counts(session)

    assert before == after  # ni core ni source_entity_map cambian
    assert stats2.created == 0
    assert stats2.resolved > 0


def test_existing_external_id_resolves_to_same_uuid(seeded) -> None:
    _seed_raw("/leagues", _load("leagues.json"))

    normalize_catalog()
    with get_session() as session:
        comp_id_1 = session.execute(select(Competition.id)).scalar_one()
        mapped_internal = session.execute(
            select(SourceEntityMap.internal_id).where(SourceEntityMap.entity_type == "competition")
        ).scalar_one()

    normalize_catalog()
    with get_session() as session:
        assert session.scalar(select(func.count()).select_from(Competition)) == 1
        comp_id_2 = session.execute(select(Competition.id)).scalar_one()

    assert comp_id_1 == comp_id_2 == mapped_internal


def test_team_with_null_venue_does_not_break(seeded) -> None:
    _seed_raw("/teams", _load("teams.json"))

    normalize_catalog()
    with get_session() as session:
        mystery = session.execute(select(Team).where(Team.name == "Mystery FC")).scalar_one()
        assert mystery.home_venue_id is None
        # Old Trafford sí se creó; el venue null no.
        assert session.scalar(select(func.count()).select_from(Venue)) == 1
