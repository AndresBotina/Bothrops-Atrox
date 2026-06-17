"""Configuración compartida de pytest.

Garantiza que exista una `DATABASE_URL` de test apuntando a una Postgres local,
de modo que la suite no dependa de un `.env` presente. Si el entorno ya define
`DATABASE_URL` (p. ej. en CI), se respeta.
"""

from __future__ import annotations

import os

# Base Postgres local de test. Coincide con el contenedor `valuebet_db` (puerto 5433).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://valuebet:valuebet@localhost:5433/valuebet",
)
