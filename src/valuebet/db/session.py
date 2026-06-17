"""Acceso a la base: fábrica de engine/sesión y healthcheck.

Las capas superiores obtienen sesiones SÓLO desde aquí, de modo que el I/O contra
Postgres queda aislado en esta capa (arquitectura §4 del CLAUDE.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from valuebet.config.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Crea (una sola vez) el engine a partir de la configuración."""
    settings = get_settings()
    # pool_pre_ping evita usar conexiones muertas tras inactividad.
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Devuelve el `sessionmaker` ligado al engine del proceso."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False, class_=Session)


@contextmanager
def get_session() -> Iterator[Session]:
    """Context manager de sesión: commitea al salir, revierte ante excepción."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def healthcheck() -> bool:
    """Ejecuta `SELECT 1` y reporta True (éxito) o False (fallo)."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
