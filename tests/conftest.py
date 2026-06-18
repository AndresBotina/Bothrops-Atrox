"""Configuración compartida de pytest.

Garantiza que exista una `DATABASE_URL` de test apuntando a una Postgres local,
de modo que la suite no dependa de un `.env` presente. Si el entorno ya define
`DATABASE_URL` (p. ej. en CI), se respeta.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Base Postgres local de test. Coincide con el contenedor `valuebet_db` (puerto 5433).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://valuebet:valuebet@localhost:5433/valuebet",
)

ROOT = Path(__file__).resolve().parents[1]


def alembic_config():
    """Config de Alembic con rutas absolutas (robusto ante el cwd)."""
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


@pytest.fixture
def migrated_db():
    """Asegura que el esquema esté aplicado (`upgrade head`), sin importar el orden.

    No revierte al terminar: otros tests reutilizan el esquema. El test de
    migraciones (HU 0.4.1) gestiona su propio downgrade.
    """
    from alembic import command

    command.upgrade(alembic_config(), "head")
    yield
