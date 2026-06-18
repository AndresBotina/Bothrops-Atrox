"""HU 1.1.1 — Idempotencia del registro de fuentes (seed_sources)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from valuebet.adapters.sources import KNOWN_SOURCES, seed_sources
from valuebet.db.models.meta import Source
from valuebet.db.session import get_session

pytestmark = pytest.mark.integration


def test_seed_sources_is_idempotent_by_code(migrated_db) -> None:
    # Dos siembras seguidas.
    with get_session() as session:
        seed_sources(session)
    with get_session() as session:
        seed_sources(session)

    with get_session() as session:
        for src in KNOWN_SOURCES:
            n = session.scalar(
                select(func.count()).select_from(Source).where(Source.code == src["code"])
            )
            assert n == 1, f"{src['code']} duplicado"
