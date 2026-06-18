"""HU 1.2.1 — Tests de la zona raw y el ciclo de vida de la corrida."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from valuebet.adapters.base import Adapter, RawFetchResult
from valuebet.adapters.sources import seed_sources
from valuebet.db.models.meta import IngestionRun
from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.raw import ingestion_run

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded(migrated_db):
    """Esquema aplicado + fuentes registradas."""
    with get_session() as session:
        seed_sources(session)
    yield


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


def test_persist_creates_payload_with_provenance(seeded) -> None:
    result = RawFetchResult.build(
        source_code="api_sports",
        endpoint="/teams",
        payload={"a": 1},
        params={"league": 39},
    )
    with ingestion_run("api_sports", "demo_flow") as run:
        payload_id = run.persist(result)

    with get_session() as session:
        row = session.get(Payload, payload_id)
        assert row is not None
        assert row.endpoint == "/teams"
        assert row.payload == {"a": 1}
        assert row.source_id is not None
        assert row.ingestion_run_id is not None
        # invariante #6: timestamp con zona (UTC).
        assert row.fetched_at.utcoffset() is not None


def test_failed_run_closes_as_failed_and_rolls_back_payloads(seeded) -> None:
    with pytest.raises(RuntimeError), ingestion_run("api_sports", "boom_flow") as run:
        run.persist(RawFetchResult.build(source_code="api_sports", endpoint="/x", payload={}))
        raise RuntimeError("explota")

    with get_session() as session:
        row = _last_run(session, "boom_flow")
        assert row.status == "failed"
        assert "explota" in row.error
        assert row.finished_at is not None
        # el payload no commiteado de la corrida fallida se descartó.
        n = session.scalar(
            select(func.count()).select_from(Payload).where(Payload.ingestion_run_id == row.id)
        )
        assert n == 0


def test_two_fetches_same_endpoint_create_two_rows(seeded) -> None:
    """Append-only: mismo endpoint (mismo request_hash) => DOS filas."""
    r1 = RawFetchResult.build(source_code="the_odds_api", endpoint="/odds", payload={"t": 1})
    r2 = RawFetchResult.build(source_code="the_odds_api", endpoint="/odds", payload={"t": 2})
    assert r1.request_hash == r2.request_hash  # mismo endpoint/params

    with ingestion_run("the_odds_api", "odds_flow") as run:
        run.persist(r1)
        run.persist(r2)
        run_id = run.run_id
        assert run.rows_written == 2

    with get_session() as session:
        n = session.scalar(
            select(func.count()).select_from(Payload).where(Payload.ingestion_run_id == run_id)
        )
        assert n == 2


def test_demo_flow_with_fake_adapter(seeded) -> None:
    """Verificación: adapter FALSO persiste en raw y deja la run en 'success'."""

    class FakeAdapter:
        source_code = "api_sports"

        def fetch(self, resource, params=None):
            return RawFetchResult.build(
                source_code=self.source_code,
                endpoint=resource,
                payload={"resource": resource},
                params=params,
            )

    adapter = FakeAdapter()
    assert isinstance(adapter, Adapter)  # cumple el contrato (Protocol)

    with ingestion_run(adapter.source_code, "demo_success") as run:
        run.persist(adapter.fetch("/teams", {"league": 39}))

    with get_session() as session:
        row = _last_run(session, "demo_success")
        assert row.status == "success"
        assert row.rows_written == 1
