"""HU 1.3.1 — Integración: el fetch de catálogo aterriza en raw dentro de una run."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from valuebet.adapters.api_football import BASE_URL, ApiFootballAdapter, ApiFootballError
from valuebet.adapters.http import HTTPClient
from valuebet.adapters.sources import seed_sources
from valuebet.db.models.meta import IngestionRun
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.raw import ingestion_run

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures" / "api_football"


@pytest.fixture
def seeded(migrated_db):
    with get_session() as session:
        seed_sources(session)
    yield


def _adapter(payload: dict, status: int = 200) -> ApiFootballAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    inner = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return ApiFootballAdapter(HTTPClient(client=inner, max_attempts=2, backoff_base=0.0))


def _last_run(session, flow_name: str) -> IngestionRun:
    return (
        session.execute(
            select(IngestionRun)
            .where(IngestionRun.flow_name == flow_name)
            .order_by(IngestionRun.started_at.desc())
        )
        .scalars()
        .first()
    )


def test_catalog_fetch_lands_in_raw_and_run_success(seeded) -> None:
    payload = json.loads((FIXTURES / "leagues.json").read_text(encoding="utf-8"))
    adapter = _adapter(payload)

    with ingestion_run("api_sports", "fetch_leagues_it", params={"country": "England"}) as run:
        payload_id = run.persist(adapter.fetch_leagues({"country": "England"}))

    with get_session() as session:
        row = session.get(Payload, payload_id)
        assert row is not None
        assert row.endpoint == "/leagues"
        assert row.payload["results"] == 1

        assert _last_run(session, "fetch_leagues_it").status == "success"


def test_application_error_marks_run_failed_and_persists_nothing(seeded) -> None:
    payload = {
        "get": "leagues",
        "parameters": {},
        "errors": {"token": "Error/Missing application key."},
        "results": 0,
        "response": [],
    }
    adapter = _adapter(payload)

    # fetch_leagues() lanza ApiFootballError ANTES de persistir; la run la cierra
    # como 'failed' y la re-lanza.
    with pytest.raises(ApiFootballError), ingestion_run("api_sports", "fetch_leagues_quota") as run:
        run.persist(adapter.fetch_leagues())

    with get_session() as session:
        failed = _last_run(session, "fetch_leagues_quota")
        assert failed.status == "failed"
        assert "token" in failed.error
        n = session.scalar(
            select(func.count()).select_from(Payload).where(Payload.ingestion_run_id == failed.id)
        )
        assert n == 0
