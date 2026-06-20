"""Chequeos de calidad de datos sobre `core` (HU 1.6.1).

SOLO LECTURA: ninguna función modifica `core`. Cada chequeo es una función pura que
recibe una `Session` y devuelve un `CheckResult` (nombre, severidad, passed, conteo,
detalles con ids de ejemplo). `run_quality_audit` los ejecuta todos bajo una corrida
`meta.ingestion_runs` (flow 'quality_audit') y persiste cada resultado en
`meta.data_quality_checks`.

Severidad:
  * error/critical → bugs de datos que NO deberían pasar (huérfanos, referencias rotas,
    valores imposibles como possession>1 o xg<0).
  * info/warning → cobertura/completitud, NO un fallo (un partido sin xG en 2015 es
    esperado).

Reglas de estado reutilizadas del pipeline (documentadas):
  * GOALS_EXPECTED = finished/aet/penalties/awarded → deben tener resultado.
    (cancelled/abandoned son terminales pero pueden no tener goles → se excluyen.)
  * PLAYED = finished/aet/penalties → partidos jugados completos; se espera que tengan
    stats. (awarded/cancelled/abandoned no necesariamente.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from valuebet.db.models.core import (
    Competition,
    Match,
    MatchStatus,
    MatchTeamStats,
    Player,
    Season,
    SourceEntityMap,
    Team,
    Venue,
)
from valuebet.db.models.meta import DataQualityCheck
from valuebet.ingestion.raw import ingestion_run

# Severidades válidas (coinciden con el CHECK de meta.data_quality_checks).
INFO, WARNING, ERROR, CRITICAL = "info", "warning", "error", "critical"

GOALS_EXPECTED = ("finished", "aet", "penalties", "awarded")
PLAYED = ("finished", "aet", "penalties")

_SAMPLE = 5


@dataclass
class CheckResult:
    name: str
    severity: str
    passed: bool
    count: int
    details: dict = field(default_factory=dict)


def _collect(session: Session, stmt) -> tuple[int, list[str]]:
    """Ejecuta un SELECT de ids y devuelve (conteo, ids_de_ejemplo)."""
    ids = list(session.execute(stmt).scalars())
    return len(ids), [str(i) for i in ids[:_SAMPLE]]


def _violation(name: str, severity: str, count: int, examples: list[str]) -> CheckResult:
    """Chequeo de violación: pasa si no encontró ninguna instancia."""
    return CheckResult(
        name=name,
        severity=severity,
        passed=count == 0,
        count=count,
        details={"count": count, "examples": examples},
    )


# --------------------------------------------------------------------------- #
# Integridad referencial y estructural
# --------------------------------------------------------------------------- #
def check_orphan_match_teams(session: Session) -> CheckResult:
    stmt = select(Match.id).where(
        or_(
            Match.home_team_id.not_in(select(Team.id)),
            Match.away_team_id.not_in(select(Team.id)),
        )
    )
    count, examples = _collect(session, stmt)
    return _violation("matches_equipos_huerfanos", CRITICAL, count, examples)


def check_terminal_without_goals(session: Session) -> CheckResult:
    stmt = select(Match.id).where(
        Match.status_code.in_(GOALS_EXPECTED),
        or_(Match.home_goals.is_(None), Match.away_goals.is_(None)),
    )
    count, examples = _collect(session, stmt)
    return _violation("matches_terminal_sin_goles", ERROR, count, examples)


def check_goals_without_terminal(session: Session) -> CheckResult:
    non_terminal = select(MatchStatus.code).where(MatchStatus.is_terminal.is_(False))
    stmt = select(Match.id).where(
        Match.status_code.in_(non_terminal),
        or_(Match.home_goals.is_not(None), Match.away_goals.is_not(None)),
    )
    count, examples = _collect(session, stmt)
    return _violation("matches_goles_sin_terminal", ERROR, count, examples)


def check_orphan_stats_match(session: Session) -> CheckResult:
    stmt = select(MatchTeamStats.id).where(MatchTeamStats.match_id.not_in(select(Match.id)))
    count, examples = _collect(session, stmt)
    return _violation("stats_match_inexistente", CRITICAL, count, examples)


_ENTITY_TABLES = {
    "competition": Competition,
    "season": Season,
    "team": Team,
    "player": Player,
    "venue": Venue,
    "match": Match,
}


def check_dangling_source_entity_map(session: Session) -> CheckResult:
    examples: list[str] = []
    count = 0
    for entity_type, model in _ENTITY_TABLES.items():
        stmt = select(SourceEntityMap.id).where(
            SourceEntityMap.entity_type == entity_type,
            SourceEntityMap.internal_id.not_in(select(model.id)),
        )
        n, ids = _collect(session, stmt)
        count += n
        examples.extend(ids)
    return _violation("source_entity_map_internal_id_roto", ERROR, count, examples[:_SAMPLE])


# --------------------------------------------------------------------------- #
# Completitud (cobertura real — info/warning, no error)
# --------------------------------------------------------------------------- #
def check_completeness_by_season(session: Session) -> CheckResult:
    def _grouped(stmt) -> dict:
        return {row[0]: row[1] for row in session.execute(stmt)}

    totals = _grouped(select(Match.season_id, func.count()).group_by(Match.season_id))
    terminals = _grouped(
        select(Match.season_id, func.count())
        .join(MatchStatus, MatchStatus.code == Match.status_code)
        .where(MatchStatus.is_terminal.is_(True))
        .group_by(Match.season_id)
    )
    with_stats = _grouped(
        select(Match.season_id, func.count(func.distinct(Match.id)))
        .join(MatchTeamStats, MatchTeamStats.match_id == Match.id)
        .group_by(Match.season_id)
    )
    with_xg = _grouped(
        select(Match.season_id, func.count(func.distinct(Match.id)))
        .join(MatchTeamStats, MatchTeamStats.match_id == Match.id)
        .where(MatchTeamStats.xg.is_not(None))
        .group_by(Match.season_id)
    )
    labels = {
        row[0]: (row[1], row[2])
        for row in session.execute(
            select(Season.id, Competition.name, Season.label).join(
                Competition, Competition.id == Season.competition_id
            )
        )
    }

    by_season = []
    for season_id, n_matches in sorted(totals.items(), key=lambda kv: labels.get(kv[0], ("", ""))):
        competition, label = labels.get(season_id, (None, None))
        by_season.append(
            {
                "competition": competition,
                "season": label,
                "n_matches": n_matches,
                "n_terminal": terminals.get(season_id, 0),
                "n_with_stats": with_stats.get(season_id, 0),
                "n_with_xg": with_xg.get(season_id, 0),
            }
        )

    return CheckResult(
        name="completitud_por_temporada",
        severity=INFO,
        passed=True,  # reporte de cobertura, no pasa/falla
        count=sum(totals.values()),
        details={"by_season": by_season, "n_seasons": len(by_season)},
    )


def check_terminal_without_stats(session: Session) -> CheckResult:
    stmt = select(Match.id).where(
        Match.status_code.in_(PLAYED),
        Match.id.not_in(select(MatchTeamStats.match_id)),
    )
    count, examples = _collect(session, stmt)
    result = _violation("partidos_jugados_sin_stats", WARNING, count, examples)
    return result


def check_terminal_with_stats_without_xg(session: Session) -> CheckResult:
    with_stats = select(MatchTeamStats.match_id)
    with_xg = select(MatchTeamStats.match_id).where(MatchTeamStats.xg.is_not(None))
    stmt = select(Match.id).where(
        Match.status_code.in_(PLAYED),
        Match.id.in_(with_stats),
        Match.id.not_in(with_xg),
    )
    count, examples = _collect(session, stmt)
    # Esperado en temporadas < 2023: es cobertura, no un fallo.
    return CheckResult(
        name="partidos_con_stats_sin_xg",
        severity=INFO,
        passed=True,
        count=count,
        details={"count": count, "examples": examples},
    )


def check_teams_without_matches(session: Session) -> CheckResult:
    stmt = select(Team.id).where(
        Team.id.not_in(select(Match.home_team_id)),
        Team.id.not_in(select(Match.away_team_id)),
    )
    count, examples = _collect(session, stmt)
    return CheckResult(
        name="equipos_sin_partidos",
        severity=INFO,
        passed=True,
        count=count,
        details={"count": count, "examples": examples},
    )


# --------------------------------------------------------------------------- #
# Consistencia de valores
# --------------------------------------------------------------------------- #
def check_possession_out_of_range(session: Session) -> CheckResult:
    stmt = select(MatchTeamStats.id).where(
        or_(MatchTeamStats.possession < 0, MatchTeamStats.possession > 1)
    )
    count, examples = _collect(session, stmt)
    return _violation("possession_fuera_de_rango", ERROR, count, examples)


def check_negative_xg(session: Session) -> CheckResult:
    stmt = select(MatchTeamStats.id).where(MatchTeamStats.xg < 0)
    count, examples = _collect(session, stmt)
    return _violation("xg_negativo", ERROR, count, examples)


def check_negative_goals(session: Session) -> CheckResult:
    stmt = select(Match.id).where(or_(Match.home_goals < 0, Match.away_goals < 0))
    count, examples = _collect(session, stmt)
    return _violation("goles_negativos", ERROR, count, examples)


def check_suspicious_high_goals(session: Session) -> CheckResult:
    stmt = select(Match.id).where(or_(Match.home_goals > 20, Match.away_goals > 20))
    count, examples = _collect(session, stmt)
    return _violation("goles_sospechosamente_altos", WARNING, count, examples)


def check_terminal_without_kickoff(session: Session) -> CheckResult:
    terminal = select(MatchStatus.code).where(MatchStatus.is_terminal.is_(True))
    stmt = select(Match.id).where(Match.status_code.in_(terminal), Match.kickoff_utc.is_(None))
    count, examples = _collect(session, stmt)
    return _violation("terminal_sin_kickoff", ERROR, count, examples)


# ---- pandera ----------------------------------------------------------------
_STATS_COLUMNS = (
    "possession",
    "xg",
    "xga",
    "shots",
    "shots_on_tgt",
    "corners",
    "fouls",
    "yellow_cards",
    "red_cards",
)

# Contrato de rangos de match_team_stats: possession en [0,1], xG y enteros >= 0
# (o nulos). Valida la materia prima estadística.
STATS_SCHEMA = DataFrameSchema(
    {
        "possession": Column(float, Check.in_range(0.0, 1.0), nullable=True, required=False),
        "xg": Column(float, Check.greater_than_or_equal_to(0.0), nullable=True, required=False),
        "xga": Column(float, Check.greater_than_or_equal_to(0.0), nullable=True, required=False),
        "shots": Column(float, Check.greater_than_or_equal_to(0), nullable=True, required=False),
        "shots_on_tgt": Column(
            float, Check.greater_than_or_equal_to(0), nullable=True, required=False
        ),
        "corners": Column(float, Check.greater_than_or_equal_to(0), nullable=True, required=False),
        "fouls": Column(float, Check.greater_than_or_equal_to(0), nullable=True, required=False),
        "yellow_cards": Column(
            float, Check.greater_than_or_equal_to(0), nullable=True, required=False
        ),
        "red_cards": Column(
            float, Check.greater_than_or_equal_to(0), nullable=True, required=False
        ),
    },
    coerce=True,
)


def _stats_dataframe(session: Session) -> pd.DataFrame:
    cols = [MatchTeamStats.id, *[getattr(MatchTeamStats, c) for c in _STATS_COLUMNS]]
    rows = session.execute(select(*cols)).all()
    return pd.DataFrame(rows, columns=["id", *_STATS_COLUMNS])


def check_stats_pandera(session: Session) -> CheckResult:
    df = _stats_dataframe(session)
    if df.empty:
        return CheckResult("match_team_stats_pandera", ERROR, True, 0, {"rows": 0})
    try:
        STATS_SCHEMA.validate(df, lazy=True)
    except pa.errors.SchemaErrors as exc:
        failures = exc.failure_cases
        return CheckResult(
            name="match_team_stats_pandera",
            severity=ERROR,
            passed=False,
            count=int(len(failures)),
            details={
                "count": int(len(failures)),
                "examples": failures.head(_SAMPLE).astype(str).to_dict(orient="records"),
            },
        )
    return CheckResult("match_team_stats_pandera", ERROR, True, 0, {"rows": int(len(df))})


# --------------------------------------------------------------------------- #
# Registro y corrida de auditoría
# --------------------------------------------------------------------------- #
ALL_CHECKS = (
    # integridad
    check_orphan_match_teams,
    check_terminal_without_goals,
    check_goals_without_terminal,
    check_orphan_stats_match,
    check_dangling_source_entity_map,
    # completitud
    check_completeness_by_season,
    check_terminal_without_stats,
    check_terminal_with_stats_without_xg,
    check_teams_without_matches,
    # consistencia
    check_possession_out_of_range,
    check_negative_xg,
    check_negative_goals,
    check_suspicious_high_goals,
    check_terminal_without_kickoff,
    check_stats_pandera,
)


@dataclass
class AuditResult:
    results: list[CheckResult]
    run_id: object | None = None


def run_quality_audit(*, sessionmaker_=None) -> AuditResult:
    """Ejecuta todos los chequeos y los registra en meta.data_quality_checks."""
    audit = AuditResult(results=[])
    with ingestion_run("api_sports", "quality_audit", sessionmaker_=sessionmaker_) as run:
        session = run.session
        for check in ALL_CHECKS:
            result = check(session)
            audit.results.append(result)
            session.add(
                DataQualityCheck(
                    ingestion_run_id=run.run_id,
                    check_name=result.name,
                    severity=result.severity,
                    passed=result.passed,
                    details=result.details,
                )
            )
        session.flush()
        run.rows_written = len(audit.results)
        audit.run_id = run.run_id
    return audit


def export_audit_csv(results: list[CheckResult], path: Path) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["check", "severity", "passed", "count", "details"])
        for r in results:
            writer.writerow([r.name, r.severity, r.passed, r.count, r.details])
