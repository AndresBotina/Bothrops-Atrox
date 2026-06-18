# valuebet

Sistema personal de **detección de valor** en apuestas de fútbol: busca
ineficiencias del mercado donde la probabilidad estimada por el modelo supera a
la probabilidad implícita en la cuota. El norte son CLV, ROI y calibración — no
el % de acierto.

La fuente de verdad sobre cómo funciona el repo es `docs/CLAUDE.md` (la
constitución). La planificación por historias está en `docs/backlog.md`.

## Requisitos

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) como gestor de paquetes
- PostgreSQL (local o Supabase)

## Puesta en marcha

```bash
uv sync                       # crea el entorno e instala dependencias
cp .env.example .env          # configura tus credenciales (no se versiona)
uv run alembic upgrade head   # aplica el esquema a la base
uv run pytest                 # corre la suite
```

## Ingesta de catálogo (API-Football)

El fetch contra [API-Football](https://www.api-football.com/) (API-Sports) necesita
una clave. Consíguela en el dashboard y ponla en `.env` (nunca en código ni en git):

```bash
# .env
APISPORTS_KEY=tu_clave_aqui
```

Registra las fuentes y trae el catálogo crudo a `raw.payloads`:

```bash
uv run valuebet sources seed                       # registra las fuentes (idempotente)
uv run valuebet fetch leagues --country England     # catálogo de ligas -> raw
uv run valuebet fetch teams --league 39 --season 2023  # equipos de una liga/temporada -> raw
```

Cada fetch deja el payload crudo en `raw.payloads` ligado a una corrida en
`meta.ingestion_runs`. El tier gratuito limita a ~10 req/min; el cliente HTTP
respeta ese intervalo automáticamente.

### Flujo: fetch → normalize

Una vez que el catálogo crudo está en `raw`, normalízalo a las entidades `core`:

```bash
uv run valuebet normalize catalog   # raw (último /leagues y /teams) -> core
```

`normalize catalog` lee el payload más reciente de `raw` (no vuelve a la API),
valida con Pydantic y proyecta a `core.countries`, `core.competitions`,
`core.seasons`, `core.teams` y `core.venues`, resolviendo identidad vía
`core.source_entity_map` (external_id de la API → UUID interno). Es **idempotente**:
re-ejecutarlo no duplica entidades. `raw` queda intacto (esta capa sólo lee de
`raw` y escribe en `core`).

Con el catálogo ya en `core`, ingesta y normaliza los partidos:

```bash
uv run valuebet fetch fixtures --league 39 --season 2023   # partidos -> raw
uv run valuebet normalize fixtures                          # raw -> core.matches
```

`normalize fixtures` mapea `fixture.status.short` a `core.match_statuses`, deriva
`kickoff_utc` en UTC y sólo asigna goles en estados con resultado (finished/aet/
penalties/abandoned/awarded). Resuelve season/equipos/venue por identidad (el
catálogo es prerequisito: no crea entidades fantasma) y hace **UPSERT** por
`fixture.id`, así que un partido que pasa de `scheduled` a `finished` se actualiza
en su sitio. Los estados desconocidos y los partidos no-normalizables se registran
en `meta.data_quality_checks` en vez de romper o meter basura.

## Estructura (src-layout)

```
src/valuebet/
  config/      settings y logging
  db/          engine, sesión y modelos SQLAlchemy
  adapters/    patrón Adapter por fuente de datos
  ingestion/   raw -> core -> market
  domain/      entidades / modelos Pydantic compartidos
  features/    feature engineering (Fase 2+)
  modeling/    modelo Dixon-Coles (Fase 3)
  betting/     señales de valor y staking (Fase 4)
  evaluation/  backtesting walk-forward (Fase 2)
  api/         FastAPI (delgado)
  cli/         comandos Typer
migrations/    migraciones Alembic
tests/
docs/
```
