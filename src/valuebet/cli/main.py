"""Definición de los comandos Typer de valuebet."""

from __future__ import annotations

from pathlib import Path

import typer

from valuebet.adapters.api_football import open_adapter
from valuebet.adapters.sources import seed_sources
from valuebet.config.settings import Settings, get_settings
from valuebet.evaluation.backtest import walk_forward
from valuebet.evaluation.baselines import BASELINES, build_model
from valuebet.evaluation.data import load_matches
from valuebet.evaluation.metrics import BacktestMetrics, evaluate
from valuebet.evaluation.store import persist_backtest
from valuebet.evaluation.tuning import DEFAULT_HALF_LIFE_GRID, best_by_brier, sweep_half_lives
from valuebet.ingestion.backfill import run_backfill
from valuebet.ingestion.coverage import coverage_report, export_csv, verify_xg
from valuebet.ingestion.normalize_catalog import normalize_catalog
from valuebet.ingestion.normalize_fixtures import normalize_fixtures
from valuebet.ingestion.normalize_stats import normalize_stats
from valuebet.ingestion.orchestrate import ingest_league_season
from valuebet.ingestion.raw import ingestion_run
from valuebet.quality.checks import export_audit_csv, run_quality_audit

app = typer.Typer(help="valuebet — detección de valor en apuestas de fútbol.")
sources_app = typer.Typer(help="Gestión del registro de fuentes (meta.sources).")
fetch_app = typer.Typer(help="Fetch de datos crudos hacia raw.payloads.")
normalize_app = typer.Typer(help="Normalización de raw hacia core.")
ingest_app = typer.Typer(help="Flujos de ingesta encadenados (fetch → raw → normalize).")
discover_app = typer.Typer(help="Descubrimiento de catálogo (catálogo global de ligas).")
quality_app = typer.Typer(help="Auditoría de calidad de datos sobre core (solo lectura).")
app.add_typer(sources_app, name="sources")
app.add_typer(fetch_app, name="fetch")
app.add_typer(normalize_app, name="normalize")
app.add_typer(ingest_app, name="ingest")
app.add_typer(discover_app, name="discover")
app.add_typer(quality_app, name="quality")


@sources_app.command("seed")
def sources_seed() -> None:
    """Siembra/actualiza las fuentes conocidas en meta.sources (idempotente)."""
    n = seed_sources()
    typer.echo(f"Fuentes sembradas/actualizadas: {n}")


def _yn(value: bool) -> str:
    return "Y" if value else "."


def _parse_int_list(raw: str) -> list[int]:
    """Parsea '39,140' o rangos '2015-2024' (o mezcla 'a,b-c') a una lista de ints."""
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo, hi = token.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(token))
    return out


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


@ingest_app.command("league-season")
def ingest_league_season_cmd(
    league: int = typer.Option(..., "--league", help="ID de liga de API-Football."),
    season: int = typer.Option(..., "--season", help="Temporada (año)."),
    request_budget: int = typer.Option(
        None, "--request-budget", help="Tope de peticiones para esta corrida."
    ),
    skip_existing: bool = typer.Option(
        True,
        "--skip-existing/--no-skip-existing",
        help="Evita refetch de lo ya presente en core (ahorra cuota).",
    ),
) -> None:
    """Encadena catálogo → partidos → stats para una (liga, temporada)."""
    settings = get_settings()
    key = _require_api_key(settings)

    with open_adapter(key) as adapter:
        summary = ingest_league_season(
            adapter,
            league,
            season,
            request_budget=request_budget,
            skip_existing=skip_existing,
        )

    if summary.catalog_skipped:
        cat = "omitido"
    else:
        cat = f"nuevas={summary.catalog.created}, resueltas={summary.catalog.resolved}"
    typer.echo(f"ingest league={league} season={season} → {summary.status.upper()}")
    typer.echo(f"  catálogo: {cat}")
    typer.echo(
        f"  partidos: nuevos={summary.fixtures.created}, actualizados={summary.fixtures.updated}"
    )
    typer.echo(
        f"  stats: filas={summary.stats.rows_upserted}, "
        f"sin_stats={summary.stats.matches_without_stats}, "
        f"ya_existentes={summary.stats_skipped_existing}, "
        f"pendientes={summary.stats_pending}, fallidos={summary.stats_failed}"
    )
    typer.echo(f"  peticiones consumidas: {summary.requests_made}")


@discover_app.command("leagues")
def discover_leagues() -> None:
    """Trae el catálogo GLOBAL de ligas (sin filtro de país) a raw. Cuesta 1 petición."""
    settings = get_settings()
    key = _require_api_key(settings)

    with (
        open_adapter(key) as adapter,
        ingestion_run("api_sports", "discover_leagues") as run,
    ):
        result = adapter.fetch_leagues()  # sin params → todas las ligas del mundo
        payload_id = run.persist(result)

    typer.echo(
        f"discover leagues: results={result.payload.get('results')} → raw.payloads {payload_id}"
    )


@app.command("coverage")
def coverage(
    country: str = typer.Option(None, "--country", help="Filtra por país (nombre exacto)."),
    xg_probable: bool = typer.Option(
        False, "--xg-probable", help="Sólo ligas con coverage que hace xG probable."
    ),
    season_min: int = typer.Option(
        None, "--season-min", help="Sólo ligas con temporada más reciente >= año."
    ),
    csv_path: str = typer.Option(None, "--csv", help="Exporta la tabla a un CSV."),
) -> None:
    """Lista la cobertura por liga (lee el último /leagues de raw; no llama a la API)."""
    rows = coverage_report(country=country, xg_probable=xg_probable, season_min=season_min)
    if not rows:
        typer.echo("Sin datos: corre 'valuebet discover leagues' primero.")
        raise typer.Exit(code=0)

    header = (
        f"{'id':>6}  {'xg':<3} {'country':<18} {'league':<30} {'seas':>4} "
        f"{'ev':<3}{'lu':<3}{'sf':<3}{'sp':<3}{'std':<4}{'odd':<4}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        typer.echo(
            f"{r.league_id:>6}  {('YES' if r.xg_probable else 'no'):<3} "
            f"{(r.country or '')[:18]:<18} {r.name[:30]:<30} {str(r.season or ''):>4} "
            f"{_yn(r.events):<3}{_yn(r.lineups):<3}{_yn(r.statistics_fixtures):<3}"
            f"{_yn(r.statistics_players):<3}{_yn(r.standings):<4}{_yn(r.odds):<4}"
        )
    typer.echo(f"\n{len(rows)} ligas (xg_probable={sum(1 for r in rows if r.xg_probable)}).")

    if csv_path is not None:
        export_csv(rows, Path(csv_path))
        typer.echo(f"CSV exportado a {csv_path}")


@app.command("backfill")
def backfill(
    leagues: str = typer.Option(..., "--leagues", help="IDs de liga: '39,140'."),
    seasons: str = typer.Option(
        ..., "--seasons", help="Años: lista '2022,2023' o rango '2015-2024' (o mezcla)."
    ),
    request_budget: int = typer.Option(
        None, "--request-budget", help="Tope DURO de peticiones para todo el lote."
    ),
    skip_existing: bool = typer.Option(
        True, "--skip-existing/--no-skip-existing", help="Salta lo ya completo en core."
    ),
) -> None:
    """Backfill histórico reanudable sobre varias (liga, temporada)."""
    settings = get_settings()
    key = _require_api_key(settings)
    targets = [(lg, ss) for lg in _parse_int_list(leagues) for ss in _parse_int_list(seasons)]

    with open_adapter(key) as adapter:
        summary = run_backfill(
            adapter, targets, request_budget=request_budget, skip_existing=skip_existing
        )

    typer.echo(f"backfill → {summary.status.upper()} ({len(targets)} targets)")
    typer.echo(
        f"{'league':>7} {'season':>6}  {'estado':<9} {'partidos':>8} {'stats':>6} {'reqs':>5}"
    )
    typer.echo("-" * 50)
    for t in summary.targets:
        typer.echo(
            f"{t.league_id:>7} {t.season:>6}  {t.state:<9} {t.n_matches:>8} "
            f"{t.n_stats_rows:>6} {t.requests:>5}"
        )
    typer.echo(f"\npeticiones consumidas: {summary.requests_made}")
    if summary.quota_exceeded:
        typer.secho(
            "DETENIDO por límite de cuota de la API; reanuda tras el reset diario "
            "con el mismo comando.",
            fg=typer.colors.YELLOW,
        )
    pendientes = [
        f"{t.league_id}/{t.season}" for t in summary.targets if t.state in ("partial", "pending")
    ]
    if pendientes:
        typer.echo(f"falta (re-ejecuta para continuar): {', '.join(pendientes)}")


@quality_app.command("audit")
def quality_audit(
    csv_path: str = typer.Option(None, "--csv", help="Exporta el detalle de la auditoría a CSV."),
) -> None:
    """Corre todos los chequeos de calidad sobre core y los registra (no llama a la API)."""
    audit = run_quality_audit()

    header = f"{'severidad':<9} {'ok':<3} {'conteo':>7}  chequeo"
    typer.echo(header)
    typer.echo("-" * 60)
    for r in audit.results:
        ok = "OK" if r.passed else "!!"
        color = None
        if not r.passed and r.severity in ("error", "critical"):
            color = typer.colors.RED
        elif not r.passed and r.severity == "warning":
            color = typer.colors.YELLOW
        typer.secho(f"{r.severity:<9} {ok:<3} {r.count:>7}  {r.name}", fg=color)

    problemas = [r for r in audit.results if not r.passed and r.severity in ("error", "critical")]
    typer.echo(f"\nchequeos: {len(audit.results)} · con error/critical: {len(problemas)}")

    # Foto de cobertura por temporada.
    cobertura = next((r for r in audit.results if r.name == "completitud_por_temporada"), None)
    if cobertura and cobertura.details.get("by_season"):
        typer.echo(
            "\ncobertura por temporada (competición · temporada · partidos/terminales/stats/xG):"
        )
        for row in cobertura.details["by_season"]:
            typer.echo(
                f"  {row['competition']} · {row['season']}: "
                f"{row['n_matches']}/{row['n_terminal']}/{row['n_with_stats']}/{row['n_with_xg']}"
            )

    if csv_path is not None:
        export_audit_csv(audit.results, Path(csv_path))
        typer.echo(f"\nCSV exportado a {csv_path}")


@app.command("verify-xg")
def verify_xg_cmd(
    league: int = typer.Option(..., "--league", help="ID de liga de API-Football."),
    season: int = typer.Option(..., "--season", help="Temporada (año)."),
) -> None:
    """Confirma con datos REALES si una liga-temporada entrega xG. Cuesta ~2 peticiones."""
    settings = get_settings()
    key = _require_api_key(settings)

    with open_adapter(key) as adapter:
        result = verify_xg(adapter, league, season)

    veredicto = "sí" if result.xg_real else "no"
    typer.echo(f"Liga {league} temporada {season}: xG REAL = {veredicto}")
    if result.fixture_id is not None:
        typer.echo(
            f"  fixture de muestra: {result.fixture_id}, expected_goals={result.example_value}"
        )
    if result.note:
        typer.echo(f"  nota: {result.note}")


def _render_backtest_report(metrics: BacktestMetrics) -> None:
    """Imprime métricas de CALIBRACIÓN y la tabla de fiabilidad."""
    typer.echo(f"predicciones evaluadas: {metrics.n}")
    typer.echo("\nmétricas de calibración (menor es mejor; el norte NO es el acierto):")
    typer.echo(f"  Brier score : {metrics.brier:.4f}   (0 = perfecto, máx 2)")
    typer.echo(f"  Log loss    : {metrics.log_loss:.4f}")
    typer.echo(f"  ECE         : {metrics.ece:.4f}   (error de calibración medio)")
    typer.secho(
        f"  Accuracy    : {metrics.accuracy:.4f}   (REFERENCIA, NO es la métrica objetivo)",
        fg=typer.colors.YELLOW,
    )

    typer.echo("\ntabla de fiabilidad (prob. predicha vs frecuencia real, one-vs-rest):")
    typer.echo(f"  {'tramo':<14}{'n':>7}{'predicho':>11}{'observado':>11}")
    typer.echo("  " + "-" * 41)
    for b in metrics.calibration:
        if b.count == 0:
            continue
        typer.echo(
            f"  [{b.lower:.1f}, {b.upper:.1f}){'':<3}{b.count:>7}"
            f"{b.mean_predicted:>11.3f}{b.observed_freq:>11.3f}"
        )


def _parse_half_life_grid(raw: str) -> tuple[float | None, ...]:
    """Parsea '90,180,365,inf' a una rejilla; 'inf'/'none'/'-' → None (sin decaimiento)."""
    grid: list[float | None] = []
    for token in raw.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token in ("inf", "infinito", "none", "-"):
            grid.append(None)
        else:
            grid.append(float(token))
    return tuple(grid)


def _hl_label(half_life: float | None) -> str:
    return "infinito" if half_life is None else f"{half_life:g}"


def _render_sweep_table(rows, model: str) -> None:
    """Imprime la tabla de barrido de half-lives (Brier/log loss por configuración)."""
    typer.echo(f"\nbarrido de half-life (días) · modelo {model}:")
    typer.echo(f"  {'half-life':>10}{'Brier':>10}{'log loss':>11}{'ECE':>9}{'n':>7}")
    typer.echo("  " + "-" * 45)
    best = best_by_brier(rows)
    for r in rows:
        marca = "  <- mejor Brier" if r is best else ""
        typer.echo(
            f"  {_hl_label(r.half_life):>10}{r.metrics.brier:>10.4f}"
            f"{r.metrics.log_loss:>11.4f}{r.metrics.ece:>9.4f}{r.metrics.n:>7}{marca}"
        )
    typer.secho(
        f"\nmejor half-life por Brier: {_hl_label(best.half_life)} días "
        f"(Brier={best.metrics.brier:.4f}).",
        fg=typer.colors.GREEN,
    )
    typer.secho(
        "AVISO: elegir el half-life mirando el Brier de TODO el histórico y reportarlo "
        "es sobreajuste sutil (lookahead en la selección). Esta tabla es para inspección; "
        "la selección rigurosa requiere un periodo de validación separado.",
        fg=typer.colors.YELLOW,
    )


@app.command("backtest")
def backtest_cmd(
    league: int = typer.Option(..., "--league", help="ID de liga de API-Football (39, 140…)."),
    model: str = typer.Option("baseline", "--model", help=f"Modelo: {', '.join(BASELINES)}."),
    train_window: int = typer.Option(
        None, "--train-window", help="Ventana DESLIZANTE de N partidos previos (omitir=expansiva)."
    ),
    from_season: int = typer.Option(
        None, "--from-season", help="Sólo temporadas con etiqueta >= este año."
    ),
    step: int = typer.Option(
        1, "--step", help="Cada cuántas predicciones se re-entrena el modelo."
    ),
    min_train: int = typer.Option(
        20, "--min-train", help="Mínimo de partidos de historia para emitir predicción."
    ),
    half_life: float = typer.Option(
        None, "--half-life", help="Half-life en DÍAS de la ponderación temporal (poisson/dc)."
    ),
    half_life_grid: str = typer.Option(
        None,
        "--half-life-grid",
        help="Modo BARRIDO: lista de half-lives en días, p.ej. '90,180,365,inf'. "
        "Vacío usa la rejilla por defecto. No persiste.",
    ),
    sweep: bool = typer.Option(
        False, "--sweep", help="Barre la rejilla de half-lives por defecto (poisson/dc)."
    ),
    n_bins: int = typer.Option(10, "--bins", help="Nº de tramos de la tabla de calibración."),
    persist: bool = typer.Option(
        True, "--persist/--no-persist", help="Guarda el backtest en models.* (idempotente)."
    ),
) -> None:
    """Corre el walk-forward de un modelo sobre los datos reales y reporta calibración.

    NO mide ROI ni CLV (faltan cuotas, Fase 4): sólo calidad/calibración de la
    predicción. El baseline es la vara mínima que cualquier modelo debe superar.

    Con --sweep / --half-life-grid barre varios half-lives y reporta una tabla
    Brier/log loss para ELEGIR el decaimiento empíricamente.
    """
    try:
        matches = load_matches(league, from_season=from_season)
    except LookupError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not matches:
        typer.secho("Sin partidos evaluables para esa liga/temporada.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    # --- Modo BARRIDO de half-lives (selección de hiperparámetro) ---
    if sweep or half_life_grid is not None:
        grid = _parse_half_life_grid(half_life_grid) if half_life_grid else DEFAULT_HALF_LIFE_GRID
        rows = sweep_half_lives(
            model,
            matches,
            grid,
            train_window=train_window,
            step=step,
            min_train=min_train,
            n_bins=n_bins,
        )
        if not rows:
            typer.secho("Ningún partido evaluable en el barrido.", fg=typer.colors.YELLOW)
            raise typer.Exit(code=0)
        _render_sweep_table(rows, model)
        raise typer.Exit(code=0)

    try:
        engine = build_model(model, half_life=half_life)
    except KeyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    result = walk_forward(
        engine, matches, train_window=train_window, step=step, min_train=min_train
    )

    typer.echo(
        f"backtest league={league} model={model} → "
        f"partidos={result.n_finished}, predichos={result.n_predicted}, "
        f"sin_historia={result.n_skipped}"
    )
    if not result.records:
        typer.secho(
            "Ningún partido tuvo historia suficiente (sube los datos o baja --min-train).",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    metrics = evaluate(result.records, n_bins=n_bins)
    typer.echo("")
    _render_backtest_report(metrics)

    if persist:
        version = (
            f"{model}-tw{train_window or 'all'}-s{step}-mt{min_train}-hl{_hl_label(half_life)}"
        )
        mv_id = persist_backtest(
            result,
            metrics,
            league_external_id=league,
            name=f"baseline:{model}",
            version=version,
            algorithm=model,
            from_season=from_season,
        )
        typer.echo(f"\npersistido en models.model_versions {mv_id} (version='{version}')")


if __name__ == "__main__":
    app()
