"""Configuración compartida de pytest.

AISLAMIENTO DE LA BASE (crítico): los tests de integración migran y truncan la base,
así que JAMÁS deben tocar la base de trabajo/real. Esta config obliga a usar una base
de test SEPARADA vía `TEST_DATABASE_URL` y aborta ruidosamente si:
  * `TEST_DATABASE_URL` no está definida, o
  * apunta a la misma base que `DATABASE_URL` (la real).

Mejor que la suite no corra a que borre datos reales.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]

# Valores de .env (si existe) como respaldo de lo que no esté ya en el entorno.
_ENV_FILE = dotenv_values(ROOT / ".env")


def _resolve(name: str) -> str | None:
    """Variable de entorno (precede) o, en su defecto, el valor del .env."""
    return os.environ.get(name) or _ENV_FILE.get(name)


def _abort(message: str) -> None:
    raise RuntimeError(
        f"{message}\n"
        "Configura una base de TEST separada en TEST_DATABASE_URL (p. ej. "
        "postgresql+psycopg://valuebet:valuebet@localhost:5433/valuebet_test) — "
        "ver README y .env.example. Los tests NUNCA deben correr contra la base real."
    )


# --- Guarda de seguridad: ejecutada al recolectar, antes de cualquier fixture ---
_REAL_DB = _resolve("DATABASE_URL")
_TEST_DB = _resolve("TEST_DATABASE_URL")

if not _TEST_DB:
    _abort("TEST_DATABASE_URL no está definida; los tests apuntarían a la base real.")
if _REAL_DB and _TEST_DB == _REAL_DB:
    _abort("TEST_DATABASE_URL == DATABASE_URL: los tests apuntan a la BASE REAL.")

# A partir de aquí, TODA la app (settings, engine, alembic env.py) usa la base de test:
# la variable de entorno tiene precedencia sobre el valor de .env en pydantic-settings.
os.environ["DATABASE_URL"] = _TEST_DB


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
