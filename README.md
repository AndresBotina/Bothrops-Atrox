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
