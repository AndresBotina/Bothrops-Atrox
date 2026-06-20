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

## Descubrimiento de cobertura (qué ligas modelar)

API-Football promete cobertura (stats/eventos/lineups) pero no siempre cumple: hay
ligas que declaran estadísticas y aun así **no entregan xG real**. El flujo de
descubrimiento ayuda a decidir qué ligas valen la pena, gastando muy poca cuota:

```bash
# 1) catálogo GLOBAL de ligas con su coverage (1 sola petición)
uv run valuebet discover leagues

# 2) lista de cobertura por liga (solo lectura de raw; 0 peticiones)
uv run valuebet coverage --xg-probable --season-min 2022
uv run valuebet coverage --country Colombia --csv ligas.csv

# 3) confirmación REAL de xG sobre una candidata (~2 peticiones)
uv run valuebet verify-xg --league 39 --season 2023
```

`coverage` deriva una columna `xg_probable` = (`statistics_players` ∧ `events` ∧
`statistics_fixtures`): una **heurística**, no una garantía. Por eso `verify-xg`
trae un partido terminal real y comprueba si `expected_goals` viene poblado, con un
veredicto claro ("xG REAL = sí/no"). Coste total típico del descubrimiento:
1 (discover) + 0 (coverage) + ~2 por liga verificada.

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

### Todo de una vez: `ingest league-season`

El comando que **encadena todo** en el orden de dependencias correcto (catálogo →
partidos → stats) para una liga/temporada, en una sola corrida auditable:

```bash
uv run valuebet ingest league-season --league 39 --season 2023
uv run valuebet ingest league-season --league 39 --season 2023 --request-budget 50
uv run valuebet ingest league-season --league 39 --season 2023 --no-skip-existing
```

Todos los payloads de los sub-fetches quedan ligados a una única fila en
`meta.ingestion_runs`. `--request-budget` limita las peticiones (útil con el tier
gratuito de 100/día): al alcanzarlo, la corrida cierra en `partial` registrando lo
pendiente, sin fallar, y una corrida posterior la completa. `--skip-existing` (por
defecto) evita refetch de lo que ya está en `core`. Si el fetch de stats de un
partido falla, el flujo sigue con el resto y la corrida queda `partial`.

### Backfill histórico: `valuebet backfill`

Para acumular varias temporadas de varias ligas en un solo lote **reanudable**:

```bash
# rango de temporadas (o lista '2022,2023,2024')
uv run valuebet backfill --leagues 39,140 --seasons 2015-2024
uv run valuebet backfill --leagues 39,140 --seasons 2015-2024 --request-budget 4000
```

`backfill` recorre cada `(liga, temporada)` llamando a `ingest league-season`, bajo
una corrida padre (`flow='backfill'`) que agrupa el lote. Propiedades clave:

- **Reanudable desde el estado real en core** (no un archivo de cursor): al arrancar,
  cada `(liga, temporada)` se evalúa leyendo la base. Una está *completa* si tiene
  catálogo, partidos, y todos sus partidos terminales tienen stats (o un marcador
  `match_no_stats`). Si una corrida se interrumpe, la siguiente recalcula qué falta y
  continúa **sin re-fetchear** lo ya hecho.
- **`--request-budget` es un tope DURO de seguridad** global del lote. Nunca se excede:
  al agotarse, el backfill se detiene limpio (`partial`) y deja el resto *pendiente*;
  re-ejecutarlo lo completa. Con el plan Pro (7.500 req/día) un histórico profundo cabe
  en una corrida, pero el tope protege ante interrupciones y errores.
- **Profundidad variable por liga**: una temporada sin datos (fixtures vacíos) no es
  error — se registra como `temporada_sin_datos` en `meta.data_quality_checks` y el lote
  sigue. Una corrida posterior la salta sin gastar peticiones.

El comando imprime, por `(liga, temporada)`: estado (complete/partial/pending/no_data),
partidos, filas de stats y peticiones consumidas, y qué falta para una próxima corrida.

Los pasos siguen disponibles por separado (útiles para depurar):

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

Por último, las estadísticas post-partido (una fila por equipo por partido):

```bash
uv run valuebet fetch stats --fixture 1002    # /fixtures/statistics -> raw
uv run valuebet normalize stats               # raw -> core.match_team_stats
```

`normalize stats` sólo normaliza partidos terminales que ya existen en `core`,
inserta **una fila por equipo** con `is_home` derivado del partido, parsea la
posesión `"55%"` a fracción `0.55` y el xG a float. **La ausencia no es cero**: un
`type` que no viene (p. ej. xG en ligas sin cobertura) queda `NULL`, nunca `0`. Un
partido sin stats se registra en `meta.data_quality_checks` (no se inserta una fila
de puros NULL). Es idempotente: `UNIQUE(match_id, team_id)` → UPSERT.

## Auditoría de calidad de datos

Antes de modelar (Fase 2+) conviene saber con qué materia prima contamos. La auditoría
es **solo lectura** sobre `core` (no llama a la API) y registra sus hallazgos en
`meta.data_quality_checks`:

```bash
uv run valuebet quality audit            # imprime un reporte y persiste los chequeos
uv run valuebet quality audit --csv q.csv  # además exporta el detalle a CSV
```

Corre tres familias de chequeos:

- **Integridad** (severidad `error`/`critical`): huérfanos, referencias rotas, partidos
  terminales sin goles (o goles sin estado terminal), `source_entity_map` con
  `internal_id` inexistente.
- **Consistencia** (`error`/`warning`): `possession` fuera de `[0,1]`, `xg` negativo,
  goles negativos o absurdamente altos (>20), y un **schema `pandera`** que valida los
  rangos de `match_team_stats`.
- **Completitud** (`info`/`warning`, es cobertura, no un fallo): foto por `(liga,
  temporada)` de partidos / terminales / con stats / con xG; partidos jugados sin stats;
  partidos con stats pero sin xG (esperado en temporadas < 2023); equipos sin partidos.

La severidad distingue **bugs de datos** (que no deberían pasar) de **cobertura
esperada** (un partido sin xG en 2015 es normal). El detalle de cada chequeo incluye
conteos e ids de ejemplo.

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
