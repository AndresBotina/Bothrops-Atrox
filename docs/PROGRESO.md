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
- [ ] HU 1.4.1  Adapter de cuotas + snapshots
- [x] HU 1.5.1  Flujo de ingesta encadenado
- [ ] HU 1.5.2  Backfill histórico reanudable
- [ ] HU 1.6.1  Chequeos de calidad
- [ ] HU 1.6.2  Reporte de cobertura

## Fases 2-6: SIN EMPEZAR
Evaluador/backtesting · Dixon-Coles · Detección de valor · Retroalimentación · Contexto

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