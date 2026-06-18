"""HU 1.3.1 — Tests del adapter de catálogo de API-Football (sin API real)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from valuebet.adapters.api_football import (
    BASE_URL,
    ApiFootballAdapter,
    ApiFootballError,
)
from valuebet.adapters.base import Adapter, RawFetchResult
from valuebet.adapters.http import HTTPClient
from valuebet.adapters.schemas.api_football import parse_leagues, parse_teams

FIXTURES = Path(__file__).parent / "fixtures" / "api_football"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _adapter(payload: dict, status: int = 200) -> ApiFootballAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    inner = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return ApiFootballAdapter(HTTPClient(client=inner, max_attempts=2, backoff_base=0.0))


def test_adapter_satisfies_protocol() -> None:
    assert isinstance(_adapter(_load("leagues.json")), Adapter)


def test_fetch_leagues_valid_returns_well_formed_result() -> None:
    result = _adapter(_load("leagues.json")).fetch_leagues({"country": "England"})

    assert isinstance(result, RawFetchResult)
    assert result.source_code == "api_sports"
    assert result.endpoint == "/leagues"
    assert result.http_status == 200
    assert result.payload["results"] == 1
    assert result.fetched_at.utcoffset() is not None  # UTC (invariante #6)

    # La validación Pydantic del catálogo funciona.
    leagues = parse_leagues(result.payload)
    assert len(leagues) == 1
    entry = leagues[0]
    assert entry.league.id == 39
    assert entry.league.name == "Premier League"
    assert entry.country.name == "England"
    assert entry.seasons[0].year == 2023
    assert entry.seasons[0].current is True


def test_200_with_errors_is_application_failure() -> None:
    payload = {
        "get": "leagues",
        "parameters": {},
        "errors": {"requests": "You have reached the request limit for the day"},
        "results": 0,
        "response": [],
    }
    with pytest.raises(ApiFootballError) as exc_info:
        _adapter(payload).fetch_leagues()

    assert exc_info.value.endpoint == "/leagues"
    assert "requests" in str(exc_info.value.errors)


def test_missing_response_key_is_application_failure() -> None:
    with pytest.raises(ApiFootballError):
        _adapter({"get": "leagues", "errors": []}).fetch_leagues()


def test_teams_optional_nulls_do_not_break_validation() -> None:
    result = _adapter(_load("teams.json")).fetch_teams(league_id=39, season=2023)
    assert result.endpoint == "/teams"

    teams = parse_teams(result.payload)
    assert len(teams) == 2

    full, sparse = teams
    assert full.team.founded == 1878
    assert full.venue.capacity == 76212
    # Campos nulos toleratos como opcionales.
    assert sparse.team.founded is None
    assert sparse.team.code is None
    assert sparse.venue.id is None
    assert sparse.venue.capacity is None
