"""Test de humo: el paquete importa y la configuración es cargable."""

from __future__ import annotations

import valuebet
from valuebet.config.settings import Settings, get_settings


def test_package_importable() -> None:
    """El paquete `valuebet` se importa sin error."""
    assert valuebet is not None


def test_get_settings_is_callable() -> None:
    """`get_settings()` es invocable y devuelve una `Settings` válida.

    La `DATABASE_URL` de test la inyecta `conftest.py` apuntando a la Postgres
    local; aquí sólo verificamos que la configuración se carga y se cachea.
    """
    get_settings.cache_clear()
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.log_level == "INFO"
    # La caché devuelve la misma instancia.
    assert get_settings() is settings
