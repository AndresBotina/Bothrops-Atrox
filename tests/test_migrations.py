"""HU 0.4.1 — Test de integración de migraciones.

Aplica `upgrade head` sobre la Postgres de test, verifica tablas clave y seeds, y
luego `downgrade base` dejando la base sin ningún esquema de la aplicación.
Requiere una Postgres real: marcado como `integration`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import bindparam, create_engine, text

from valuebet.config.settings import get_settings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]

APP_SCHEMAS = ("meta", "raw", "core", "market", "features", "models", "betting")

# Una tabla clave por esquema (qualified con su esquema).
KEY_TABLES = {
    "meta": "sources",
    "raw": "payloads",
    "core": "matches",
    "market": "odds_snapshots",
    "features": "match_team_features",
    "models": "model_versions",
    "betting": "paper_bets",
}


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


@pytest.fixture
def clean_db():
    """Garantiza base limpia antes y después del test."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    yield cfg
    command.downgrade(cfg, "base")


def _count_app_schemas(conn) -> int:
    stmt = text(
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name IN :schemas"
    ).bindparams(bindparam("schemas", expanding=True))
    return conn.execute(stmt, {"schemas": list(APP_SCHEMAS)}).scalar_one()


def test_upgrade_creates_tables_and_seeds_then_downgrade_cleans(clean_db) -> None:
    cfg = clean_db
    engine = create_engine(get_settings().database_url)

    # --- upgrade head ---
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        # Los 7 esquemas existen.
        assert _count_app_schemas(conn) == len(APP_SCHEMAS)

        # Cada esquema tiene su tabla clave.
        for schema, table in KEY_TABLES.items():
            exists = conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = :t)"
                ),
                {"s": schema, "t": table},
            ).scalar_one()
            assert exists, f"falta {schema}.{table}"

        # Seeds.
        n_status = conn.execute(text("SELECT count(*) FROM core.match_statuses")).scalar_one()
        n_markets = conn.execute(text("SELECT count(*) FROM market.market_types")).scalar_one()
        assert n_status == 9
        assert n_markets == 4

    # --- downgrade base ---
    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert _count_app_schemas(conn) == 0

    engine.dispose()
