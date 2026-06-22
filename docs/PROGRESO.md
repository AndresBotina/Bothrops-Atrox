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

## Fase 3 — Motor estadístico Dixon-Coles: EN CURSO
- [x] HU 3.1  Modelo POISSON básico de goles (primera capa hacia Dixon-Coles)
  - `src/valuebet/modeling/poisson.py`: `PoissonModel` implementa `PredictionModel`
    de la Fase 2 → enchufa en walk_forward SIN tocar el evaluador.
  - Modelo log-lineal (Maher): ataque/defensa por equipo + ventaja de local;
    goles Poisson independientes. MLE con scipy L-BFGS-B + gradiente analítico.
  - Identificabilidad: media de ataques = 0 (re-centrado tras optimizar).
  - Equipos sin historia → fuerza neutra (ataque medio, defensa media); no rompe.
  - Parámetros inspeccionables vía `model.parameters` (PoissonParameters).
  - Warm-start entre reajustes: backtest completo (~4160 fits) en ~25 s.
  - Registrado en build_model: `valuebet backtest --model poisson`.
  - Requiere extra `modeling` (numpy/scipy): `uv sync --extra modeling`.
  - VERIFICACIÓN end-to-end (Premier 39, 2015-2025, vía CLI, solo lectura):
    Poisson MEJORA al baseline en las métricas objetivo.
      Brier:    baseline 0.6460  →  poisson 0.5946  ✓
      Log loss: baseline 1.0677  →  poisson 1.0441  ✓
      Accuracy (ref): 0.4423 → 0.5175 ; ECE: 0.0038 → 0.0170
  - NO se implementó la corrección Dixon-Coles (HU 3.2) ni ponderación temporal (HU 3.3).
- [x] HU 3.2  Corrección Dixon-Coles sobre el Poisson
  - `src/valuebet/modeling/dixon_coles.py`: `DixonColesModel` HEREDA de `PoissonModel`
    (sin duplicar ataque/defensa/ventaja-local/MLE/warm-start) y añade ρ vía hooks
    `_objective`/`_initial_guess`/`_bounds`/`_store_solution`/`_joint_matrix`.
  - τ(x,y) corrige las 4 celdas bajas (0-0,0-1,1-0,1-1); ρ estimado por MLE con
    gradiente analítico (verificado contra numérico vía check_grad).
  - ρ inspeccionable (`model.rho` / `parameters.rho`); ρ=0 reproduce el Poisson exacto.
  - Registrado: `valuebet backtest --model dixon_coles` (alias `dc`).
  - VERIFICACIÓN end-to-end (Premier 39, 2015-2025; comparación de 3 modelos):
      Métrica   | baseline | poisson | dixon_coles
      Brier  ↓  | 0.6460   | 0.5946  | 0.5950
      LogLoss↓  | 1.0677   | 1.0441  | 1.0429
      Accuracy  | 0.4423   | 0.5175  | 0.5188
      ECE       | 0.0038   | 0.0170  | 0.0138
    ρ real estimado ≈ -0.0387 (negativo y pequeño, como en el dominio).
    HONESTO: DC mejora log loss/accuracy/ECE sobre Poisson; Brier EMPATADO
    (+0.0004, ruido en milésimas). No empeora — efecto marginal porque ρ es chico
    y sólo corrige 4 celdas. Coherente con la expectativa de la HU.
  - NO se implementó la ponderación temporal (HU 3.3).
- [x] HU 3.3  Ponderación temporal (decaimiento exponencial) — cierra Dixon-Coles clásico
  - La ponderación vive en `PoissonModel` (la heredan Poisson y DixonColes): `half_life`
    en DÍAS; peso = exp(-ξ·edad), ξ=ln(2)/half_life. Verosimilitud PONDERADA (cada
    término ×peso, incluido τ). `half_life=None` (∞) reproduce EXACTO la HU 3.2.
  - t_ref = kickoff máximo del entrenamiento (la interfaz fit no recibe as_of).
  - half_life es HIPERPARÁMETRO: NO se estima por MLE (verosimilitudes ponderadas
    no comparables entre ξ); se elige por barrido. `evaluation/tuning.py`.
  - CLI: `--half-life N`, `--sweep` y `--half-life-grid '90,180,365,inf'` (modo barrido
    con tabla Brier/log loss + aviso de lookahead en la selección).
  - VERIFICACIÓN end-to-end (Premier 39, 2015-2025): barrido de half-lives
      half-life | Brier  | log loss | ECE
         90     | 0.5908 | 1.0441   | 0.0253
        180     | 0.5847 | 1.0319   | 0.0137
        365     | 0.5844 | 1.0299   | 0.0138   <- mejor Brier
        540     | 0.5856 | 1.0296   | 0.0142
        730     | 0.5869 | 1.0315   | 0.0144
        infinito| 0.5950 | 1.0429   | 0.0138   (= HU 3.2)
    Comparación final (Brier / logloss / accuracy-ref / ECE):
      baseline      : 0.6460 / 1.0677 / 0.4423 / 0.0038
      poisson       : 0.5946 / 1.0441 / 0.5175 / 0.0170
      dc infinito   : 0.5950 / 1.0429 / 0.5188 / 0.0138
      dc half-life365: 0.5844 / 1.0299 / 0.5317 / 0.0138  <- ganador
    HIPÓTESIS CONFIRMADA: el decaimiento mejora el Brier apreciablemente
    (∞ 0.5950 → 365d 0.5844, -0.0106), mucho más que la corrección ρ (milésimas).
    Half-life ganador ≈ 365 días (1 temporada). Aviso de lookahead en la selección
    documentado (la tabla es para inspección; selección rigurosa = validación aparte).
  - CIERRA el Dixon-Coles clásico (núcleo de la Fase 3).

## Fase 4-6: SIN EMPEZAR
Detección de valor + staking · Retroalimentación/monitoreo · Contexto (alineaciones/lesiones)

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
  