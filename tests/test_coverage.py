"""Descubrimiento de cobertura: tests de coverage_report y verify_xg (sin API real)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from valuebet.adapters.api_football import BASE_URL, ApiFootballAdapter
from valuebet.adapters.http import HTTPClient
from valuebet.adapters.sources import seed_sources
from valuebet.db.models.meta import Source
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.coverage import coverage_report, extract_coverage, verify_xg

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


def _coverage_payload() -> dict:
    """Dos ligas: una con coverage rico (xG probable), otra con statistics_players=False."""

    def league(lid, name, country, *, sp, ev=True, sf=True):
        return {
            "league": {"id": lid, "name": name, "type": "League"},
            "country": {"name": country, "code": None},
            "seasons": [
                {
                    "year": 2024,
                    "current": True,
                    "coverage": {
                        "fixtures": {
                            "events": ev,
                            "lineups": True,
                            "statistics_fixtures": sf,
                            "statistics_players": sp,
                        },
                        "standings": True,
                        "odds": False,
                    },
                }
            ],
        }

    return {
        "get": "leagues",
        "parameters": {},
        "errors": [],
        "results": 2,
        "response": [
            league(39, "Premier League", "England", sp=True),
            league(239, "Primera A", "Colombia", sp=False),  # declara stats pero no players
        ],
    }


@pytest.fixture
def seeded(migrated_db):
    with get_session() as session:
        seed_sources(session)
    yield


def test_extract_derives_xg_probable() -> None:
    rows = {r.league_id: r for r in extract_coverage(_coverage_payload())}
    assert rows[39].xg_probable is True  # todos los flags clave true
    assert rows[239].xg_probable is False  # statistics_players false


def test_coverage_report_filters_by_xg_probable(seeded) -> None:
    _seed_raw("/leagues", _coverage_payload())
    rows = coverage_report(xg_probable=True)
    assert [r.league_id for r in rows] == [39]


def test_coverage_report_filters_by_country(seeded) -> None:
    _seed_raw("/leagues", _coverage_payload())
    rows = coverage_report(country="colombia")  # case-insensitive
    assert len(rows) == 1
    assert rows[0].league_id == 239
    assert rows[0].xg_probable is False


def test_coverage_report_season_min(seeded) -> None:
    _seed_raw("/leagues", _coverage_payload())
    assert len(coverage_report(season_min=2024)) == 2
    assert coverage_report(season_min=2025) == []


# --- verify-xg ---------------------------------------------------------------
def _verify_adapter(stats_payload: dict) -> ApiFootballAdapter:
    fixtures_payload = _load("fixtures.json")  # contiene un FT (1002)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fixtures":
            return httpx.Response(200, json=fixtures_payload)
        if request.url.path == "/fixtures/statistics":
            return httpx.Response(200, json=stats_payload)
        return httpx.Response(200, json={"errors": [], "results": 0, "response": []})

    inner = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return ApiFootballAdapter(HTTPClient(client=inner, max_attempts=2, backoff_base=0.0))


def test_verify_xg_yes_when_expected_goals_present(seeded) -> None:
    result = verify_xg(_verify_adapter(_load("statistics_full.json")), 39, 2023)
    assert result.xg_real is True
    assert result.example_value == "1.8"
    assert result.fixture_id == 1002  # primer terminal en fixtures.json


def test_verify_xg_no_when_expected_goals_absent(seeded) -> None:
    # statistics_no_xg.json no trae expected_goals.
    result = verify_xg(_verify_adapter(_load("statistics_no_xg.json")), 39, 2023)
    assert result.xg_real is False
    assert result.example_value is None
    assert result.fixture_id == 1002
