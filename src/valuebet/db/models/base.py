"""Base declarativa de SQLAlchemy 2.0 y convención de nombres.

La `naming_convention` se fija en la `MetaData` ANTES de declarar cualquier modelo
para que Alembic genere nombres de constraints/índices deterministas y, por tanto,
migraciones diffeables y estables entre máquinas.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Convención de nombres para PK, FK, UNIQUE, CHECK e índices.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarativa compartida por todos los modelos ORM del proyecto."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
