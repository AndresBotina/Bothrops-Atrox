"""Configuración tipada de la aplicación.

Carga la configuración desde variables de entorno (o un archivo `.env`) usando
pydantic-settings. Esto mantiene las credenciales fuera del código (invariante
§6 del CLAUDE.md) y hace que una variable obligatoria ausente falle al arranque
con un error claro, no en runtime.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del sistema cargada desde el entorno / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Cadena de conexión a Postgres. Obligatoria: sin ella el sistema no arranca.
    # Debe usar el driver psycopg3 (postgresql+psycopg://).
    database_url: str

    # Nivel de logging. Se consume en HU 0.2.2 (logging estructurado).
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        """Exige el esquema postgresql+psycopg:// (driver psycopg3)."""
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError(
                "database_url debe usar el driver psycopg3, "
                "con el formato 'postgresql+psycopg://usuario:clave@host:puerto/db'"
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la configuración cargada, cacheada para todo el proceso."""
    return Settings()  # type: ignore[call-arg]  # campos provienen del entorno
