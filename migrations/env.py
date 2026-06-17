"""Entorno de migraciones Alembic — configurado para multi-esquema.

Puntos clave de esta configuración:
  * La URL de la base se inyecta desde valuebet.config.settings (no se hardcodea
    en alembic.ini).
  * include_schemas=True para que autogenerate inspeccione los esquemas no-default.
  * include_name restringe Alembic a NUESTROS esquemas, para que jamás intente
    soltar tablas de esquemas del sistema.
  * El ciclo de vida de los esquemas (CREATE/DROP SCHEMA) vive DENTRO de la
    migración inicial — versionado y reversible — no aquí. Así un downgrade deja
    la base completamente limpia y un upgrade la reconstruye desde cero.

Dependencias: requiere que existan valuebet.config.settings.get_settings y
valuebet.db.models.Base.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from valuebet.config.settings import get_settings
from valuebet.db.models import Base

# Objeto de configuración de Alembic (lee alembic.ini).
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inyecta la URL de la base desde settings.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# Metadata objetivo para autogenerate.
target_metadata = Base.metadata

# Esquemas que este proyecto gestiona. Debe coincidir con schema.sql.
OUR_SCHEMAS = ("meta", "raw", "core", "market", "features", "models", "betting")


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Restringe Alembic a los esquemas propios; ignora los del sistema."""
    if type_ == "schema":
        return name in OUR_SCHEMAS
    return True


def run_migrations_offline() -> None:
    """Genera SQL sin conexión (modo --sql)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Aplica migraciones contra una conexión real."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
