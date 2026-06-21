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
uv run alembic upgrade head   # aplica el esquema a la base de trabajo
uv run pytest                 # corre la suite (contra la base de TEST, ver abajo)
```

### Dos bases de datos: trabajo y tests (¡no las mezcles!)

El proyecto usa **dos** bases Postgres separadas, configuradas en `.env`:

- `DATABASE_URL` → base de **trabajo/real** (donde vive el histórico descargado).
- `TEST_DATABASE_URL` → base de **tests**, desechable. La suite la **migra y trunca**,
  así que debe ser distinta de la real.

`pytest` usa **exclusivamente** `TEST_DATABASE_URL` (el `conftest.py` la fuerza). Como
red de seguridad, la suite **aborta con error claro** si `TEST_DATABASE_URL` no está
definida o si coincide con `DATABASE_URL` — mejor no correr que borrar datos reales.

Crea la base de test una sola vez:

```bash
# con el contenedor Docker del proyecto
docker exec valuebet_db createdb -U valuebet valuebet_test
# o con un Postgres local
createdb valuebet_test
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

## Backtesting (evaluador walk-forward) — Fase 2

Antes que cualquier modelo va el **evaluador** (invariante #9): ningún modelo se
considera válido hasta que el backtest walk-forward lo mide. El comando recorre
los partidos en orden cronológico y, para cada uno, entrena el modelo **sólo con
datos anteriores a su saque** y predice; luego compara con el resultado real:

```bash
uv run valuebet backtest --league 39 --model baseline --from-season 2015
uv run valuebet backtest --league 140 --model team-frequency --train-window 200 --step 10
uv run valuebet backtest --league 39 --no-persist        # no escribe en models.*
```

### Qué mide (y qué NO)

Mide **calidad de predicción y calibración**, no rentabilidad:

- **Brier score** y **log loss** (multiclase 1X2): las métricas objetivo.
  Penalizan la sobreconfianza; menor es mejor.
- **Tabla de fiabilidad / ECE**: cuando el modelo dice ~30%, ¿ocurre ~30%?
- **Accuracy**: se imprime sólo como **referencia**; está prohibido usarla como
  objetivo (§1 del `docs/CLAUDE.md`).

**NO se mide ROI ni CLV todavía**: requieren cuotas (mercado), que llegan en la
Fase 4. Esta fase sólo evalúa la *calidad* de la probabilidad.

### Los baselines son la vara de referencia

`--model baseline` (alias de `home-advantage`) predice las **frecuencias
históricas globales** de local/empate/visitante; `team-frequency` las afina por
equipo (suavizadas hacia la global). Son el "alumno tonto": cualquier modelo
serio (Dixon-Coles, Fase 3) **debe superarlos** en Brier/log loss. Por construcción
están bien calibrados, así que fijan un mínimo exigente.

### Sin lookahead, por construcción

El entrenamiento de cada predicción se obtiene con `bisect_left` sobre los saques
ordenados: sólo entran partidos con saque **estrictamente anterior** (excluye
incluso los simultáneos). No es disciplina, es imposible filtrar futuro — y hay un
test (`test_no_lookahead_training_strictly_precedes_target`) que lo verifica
espiando qué recibió `fit` antes de cada predicción.

### Persistencia (revisable)

Por defecto guarda la corrida en `models.model_versions` (una fila por backtest,
con config, rango temporal y un resumen de métricas en `notes`) y cada predicción
en `models.predictions` (3 filas por partido: home/draw/away, mercado `1x2`). Las
predicciones son la materia prima: las métricas se **recalculan** cruzándolas con
el resultado real en `core.matches`, sin duplicar datos derivados. Es **idempotente**:
re-correr con la misma `(name, version)` reemplaza las predicciones, no duplica.

## Modelo Poisson de goles — Fase 3 (HU 3.1)

Primer modelo real, primera capa hacia Dixon-Coles. Cada equipo tiene fuerza de
**ataque** y **defensa** y hay una **ventaja de local** global; los goles de cada
lado son Poisson independientes con `log λ` lineal en esas fuerzas. Los parámetros
se estiman por **máxima verosimilitud** (scipy L-BFGS-B con gradiente analítico) y
de la matriz de marcadores (0..10 por lado) se agregan las probabilidades 1X2.

Requiere el extra opcional **`modeling`** (numpy/scipy):

```bash
uv sync --extra modeling
uv run valuebet backtest --league 39 --from-season 2015 --model poisson --no-persist
```

Implementa la misma interfaz `PredictionModel` de la Fase 2, así que **enchufa en
el walk-forward sin tocarlo** (`--model poisson`). Detalles:

- **Identificabilidad**: ataque y defensa se confunden por una constante (aₜ+c,
  dₜ−c → mismas λ); se fija la media de ataques en 0 (re-centrado tras optimizar).
- **Equipos sin historia** (ascendidos, inicio de temporada): fuerza neutra
  (ataque medio, defensa media) en vez de romper.
- **Parámetros inspeccionables** tras `fit` vía `model.parameters` (ataque/defensa
  por equipo, ventaja de local) para validar que tienen sentido.
- **Warm-start** entre reajustes: el walk-forward reajusta tras cada partido, y
  partir de la solución previa hace el backtest completo (~4160 fits) viable (~25 s).

**No incluye aún** la corrección de marcadores bajos de Dixon-Coles (HU 3.2) ni la
ponderación temporal (HU 3.3).

### Resultado vs baseline (Premier, id 39, 2015–2025, walk-forward)

El Poisson **mejora al baseline en las métricas objetivo** (Brier y log loss):

| Métrica            | baseline (HU 2.1) | poisson (HU 3.1) | dixon_coles (HU 3.2) |
|--------------------|-------------------|------------------|----------------------|
| Brier score ↓      | 0.6460            | 0.5946           | 0.5950               |
| Log loss ↓         | 1.0677            | 1.0441           | **1.0429**           |
| Accuracy (ref.)    | 0.4423            | 0.5175           | **0.5188**           |
| ECE                | 0.0038            | 0.0170           | 0.0138               |

(El baseline está trivialmente calibrado porque predice la frecuencia global y
nunca se moja; el Poisson da probabilidades más afiladas que cubren todo el rango
[0,1], a costa de un ECE algo mayor pero con mejor Brier/log loss, que es el norte.)

## Corrección Dixon-Coles — Fase 3 (HU 3.2)

`DixonColesModel` hereda de `PoissonModel` y añade la función de dependencia
τ(x,y) de Dixon & Coles (1997), que corrige SÓLO las 4 celdas de marcador bajo
(0-0, 0-1, 1-0, 1-1) con un parámetro ρ estimado por MLE junto al resto:

```bash
uv run valuebet backtest --league 39 --from-season 2015 --model dixon_coles --no-persist
```

- Con ρ<0 (lo típico en fútbol) sube la probabilidad de 0-0 y 1-1 → más empates.
- Con **ρ=0 reproduce exactamente el Poisson** (consistencia testeada).
- ρ se estima con gradiente analítico (verificado contra el numérico) y es
  inspeccionable vía `model.parameters.rho` / `model.rho`.

**Resultado honesto sobre datos reales (Premier 2015–2025):** ρ ≈ **−0.0387**
(negativo y pequeño, como manda el dominio). La corrección sólo toca 4 celdas, así
que su efecto es marginal: **mejora log loss, accuracy y ECE** respecto al Poisson,
y el **Brier queda empatado** (0.5950 vs 0.5946, diferencia de 0.0004 = ruido en
milésimas). No empeora — coherente con la expectativa de que Dixon-Coles aporta
poco cuando ρ es chico. La ponderación temporal (HU 3.3) es la siguiente capa.

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
