# PROGRESO — valuebet

## Fase 0 — Fundación: COMPLETA
- Andamiaje, settings, logging, esquema completo (7 esquemas, 28 tablas),
  migración inicial reversible, sesión/healthcheck. Verificado.

## Fase 1 — Ingesta: EN CURSO
- [x] HU 1.1.1  Contrato Adapter + registro de fuentes
- [x] HU 1.1.2  Cliente HTTP resiliente
- [x] HU 1.2.1  Zona raw + ciclo de vida de corrida (append-only)
- [x] HU 1.3.1  Adapter de catálogo API-Football (fetch → raw)
- [x] HU 1.3.2  Normalización de catálogo a core (identidad + idempotencia)
- [x] HU 1.3.3  Normalización de partidos (matches)
- [x] HU 1.3.4  Normalización de stats post-partido (match_team_stats)
- [x] HU 1.4.1  Adapter de cuotas + snapshots
- [x] HU 1.5.1  Flujo de ingesta encadenado
- [x] HU 1.5.2  Backfill histórico reanudable
- [x] HU 1.6.1  Chequeos de calidad de datos
- [x] HU 1.6.2  Reporte de cobertura

## Fase 2 — Evaluador / backtesting walk-forward: EN CURSO
- [x] HU 2.1  Esqueleto del evaluador walk-forward + métricas de calibración + baseline
  - Interfaz `PredictionModel` (Protocol) en `evaluation/model.py`: `fit`/`predict_proba`.
    Dixon-Coles (Fase 3) la implementará sin tocar el evaluador.
  - Baselines: `HomeAdvantageBaseline` (frecuencia global 1X2) y `TeamFrequencyBaseline`
    (por equipo, suavizada hacia la global). Vara de referencia.
  - `walk_forward` SIN LOOKAHEAD por construcción (`bisect_left` sobre saques: sólo
    entrena con partidos estrictamente anteriores; excluye simultáneos). `step` re-entrena
    por lotes; `min_train` omite partidos sin historia suficiente.
  - Métricas: Brier y log loss multiclase, accuracy (sólo REFERENCIA), tabla de
    fiabilidad y ECE. Todo Python puro (sin numpy).
  - Persistencia en `models.model_versions` + `models.predictions` (3 sel./partido,
    mercado 1x2); métricas se RECALCULAN desde predicciones + core (no se duplican).
    Idempotente por (name, version): delete+insert.
  - CLI `valuebet backtest --league --model [--train-window --from-season --step
    --min-train --no-persist]`. Verificado sobre datos reales (Premier 39, 4160
    predicciones, baseline calibrado ECE≈0.004).
  - NO se implementó Dixon-Coles (Fase 3) ni ROI/CLV (faltan cuotas, Fase 4).

## Fases 3-6: SIN EMPEZAR
Dixon-Coles · Detección de valor · Retroalimentación · Contexto

## Notas / deuda
- Cobertura API-Football: solo Primera A (COL) tiene stats_fixtures; sin odds para COL.
  (Las pruebas con Colombia fueron solo para validar la API.)
- Deuda menor: team.country y venue.capacity no se parsean aún (ampliar Pydantic
  cuando un feature los necesite).
- Vigilancia 1.5.2: partidos con status desconocido se omiten y quedan en
  data_quality_checks (warning). Revisar esos warnings durante el backfill
  para no perder partidos en silencio.
- Pendiente Fase 2: derivar xga (= xg del rival en el mismo partido); la API no
  lo da por equipo, queda None en ingesta.
- Decisión v1 (verificada con datos reales): modelar Premier League (id 39) y
  LaLiga (id 140), temporadas 2022-2024. Ambas con xG REAL confirmado vía
  verify-xg en 2023. Ligas regulares ~380 partidos/temporada. Se modela una liga
  por modelo; escalar a más ligas reusa la misma arquitectura (solo cambia el id).
- Colombia Primera A descartada para v1: tiene stats pero NO xG (limitación de la
  liga, no del plan). El sistema soporta sumarla luego sin refactor. 
- Cobertura confirmada (Premier 39, LaLiga 140): datos básicos (goles, tiros,
  posesión) desde 2015; xG REAL solo desde 2023 (2023-2025). Backfill v1 = 2015-2025.
  Años sin xG entrenan Dixon-Coles sobre goles; xG enriquece 2023+ vía feature
  store opcional.
- Histórico descargado y verificado: Premier (39) y La Liga (140), 2015-2025,
  11 temporadas × 380 = 4180 partidos por liga (8360 total), todos con resultado.
- Aislamiento de base de tests resuelto y verificado: pytest no toca la base real.

- [x] FASE 1 COMPLETA — ingesta verificada.
- Auditoría de calidad: 0 errores/críticos. 8360 partidos, 99.98% con stats.
- xG real confirmado: completo 2023-2025 (ambas ligas), parcial La Liga 2022 (211),
  ausente 2015-2021. ~2280 partidos con xG, 8360 con goles para Dixon-Coles.
  