"""Definición de los comandos Typer de valuebet."""

from __future__ import annotations

import typer

from valuebet.adapters.api_football import open_adapter
from valuebet.adapters.sources import seed_sources
from valuebet.config.settings import Settings, get_settings
from valuebet.ingestion.normalize_catalog import normalize_catalog
from valuebet.ingestion.normalize_fixtures import normalize_fixtures
from valuebet.ingestion.normalize_stats import normalize_stats
from valuebet.ingestion.raw import ingestion_run

app = typer.Typer(help="valuebet — detección de valor en apuestas de fútbol.")
sources_app = typer.Typer(help="Gestión del registro de fuentes (meta.sources).")
fetch_app = typer.Typer(help="Fetch de datos crudos hacia raw.payloads.")
normalize_app = typer.Typer(help="Normalización de raw hacia core.")
app.add_typer(sources_app, name="sources")
app.add_typer(fetch_app, name="fetch")
app.add_typer(normalize_app, name="normalize")


@sources_app.command("seed")
def sources_seed() -> None:
    """Siembra/actualiza las fuentes conocidas en meta.sources (idempotente)."""
    n = seed_sources()
    typer.echo(f"Fuentes sembradas/actualizadas: {n}")


def _require_api_key(settings: Settings) -> str:
    if not settings.apisports_key:
        typer.secho(
            "Falta APISPORTS_KEY: configúrala en .env (ver .env.example).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return settings.apisports_key


@fetch_app.command("leagues")
def fetch_leagues(
    country: str = typer.Option(None, help="Filtrar por país (p. ej. 'England')."),
    season: int = typer.Option(None, help="Filtrar por temporada (año)."),
) -> None:
    """Trae el catálogo de ligas de API-Football y lo aterriza en raw.payloads."""
    settings = get_settings()
    key = _require_api_key(settings)
    params = {k: v for k, v in {"country": country, "season": season}.items() if v is not None}

    with (
        open_adapter(key) as adapter,
        ingestion_run("api_sports", "fetch_leagues", params=params) as run,
    ):
        result = adapter.fetch_leagues(params or None)
        payload_id = run.persist(result)

    typer.echo(f"leagues: results={result.payload.get('results')} → raw.payloads {payload_id}")


@fetch_app.command("teams")
def fetch_teams(
    league: int = typer.Option(..., help="ID de liga de API-Football."),
    season: int = typer.Option(..., help="Temporada (año)."),
) -> None:
    """Trae el catálogo de equipos (venue embebido) y lo aterriza en raw.payloads."""
    settings = get_settings()
    key = _require_api_key(settings)
    params = {"league": league, "season": season}

    with (
        open_adapter(key) as adapter,
        ingestion_run("api_sports", "fetch_teams", params=params) as run,
    ):
        result = adapter.fetch_teams(league, season)
        payload_id = run.persist(result)

    typer.echo(f"teams: results={result.payload.get('results')} → raw.payloads {payload_id}")


@fetch_app.command("fixtures")
def fetch_fixtures(
    league: int = typer.Option(..., help="ID de liga de API-Football."),
    season: int = typer.Option(..., help="Temporada (año)."),
) -> None:
    """Trae los partidos de una liga/temporada y los aterriza en raw.payloads."""
    settings = get_settings()
    key = _require_api_key(settings)
    params = {"league": league, "season": season}

    with (
        open_adapter(key) as adapter,
        ingestion_run("api_sports", "fetch_fixtures", params=params) as run,
    ):
        result = adapter.fetch_fixtures(league, season)
        payload_id = run.persist(result)

    typer.echo(f"fixtures: results={result.payload.get('results')} → raw.payloads {payload_id}")


@fetch_app.command("stats")
def fetch_stats(
    fixture: int = typer.Option(..., help="ID de fixture de API-Football."),
) -> None:
    """Trae las estadísticas post-partido de un fixture y las aterriza en raw.payloads."""
    settings = get_settings()
    key = _require_api_key(settings)
    params = {"fixture": fixture}

    with (
        open_adapter(key) as adapter,
        ingestion_run("api_sports", "fetch_stats", params=params) as run,
    ):
        result = adapter.fetch_fixture_statistics(fixture)
        payload_id = run.persist(result)

    typer.echo(f"stats: results={result.payload.get('results')} → raw.payloads {payload_id}")


@normalize_app.command("catalog")
def normalize_catalog_cmd() -> None:
    """Normaliza el catálogo (competiciones, temporadas, equipos, estadios) desde raw."""
    stats = normalize_catalog()
    typer.echo(f"catálogo normalizado: nuevas={stats.created}, resueltas={stats.resolved}")
    typer.echo(f"  nuevas por tipo: {dict(stats.created_by_type)}")


@normalize_app.command("fixtures")
def normalize_fixtures_cmd() -> None:
    """Normaliza los partidos (core.matches) desde el último raw /fixtures."""
    stats = normalize_fixtures()
    typer.echo(
        f"partidos normalizados: nuevos={stats.created}, actualizados={stats.updated}, "
        f"omitidos={stats.skipped}"
    )
    if stats.issues:
        typer.echo(f"  incidencias (ver data_quality_checks): {stats.issues}")


@normalize_app.command("stats")
def normalize_stats_cmd() -> None:
    """Normaliza las estadísticas post-partido (core.match_team_stats) desde raw."""
    result = normalize_stats()
    typer.echo(
        f"stats normalizadas: filas={result.rows_upserted}, "
        f"partidos_sin_stats={result.matches_without_stats}, omitidos={result.skipped}"
    )
    if result.issues:
        typer.echo(f"  incidencias (ver data_quality_checks): {result.issues}")


if __name__ == "__main__":
    app()
