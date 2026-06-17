"""Modelos declarativos de SQLAlchemy 2.0 — un módulo por esquema.

Importar este paquete registra TODAS las tablas en `Base.metadata`, de modo que
Alembic (autogenerate) y `create_all` las vean. La `Base` y su `naming_convention`
viven en `base.py`.
"""

from __future__ import annotations

from valuebet.db.models import (  # noqa: F401  (importadas por su efecto de registro)
    betting,
    core,
    features,
    market,
    meta,
    models,
    raw,
)
from valuebet.db.models.base import Base

__all__ = ["Base"]
