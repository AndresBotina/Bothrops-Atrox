"""Diagnóstico de COBERTURA de ligas (solo lectura) — utilidad de descubrimiento.

NO altera la normalización ni el flujo de ingesta. Sirve para decidir qué ligas
modelar:
  * `coverage_report` lee el payload global de /leagues más reciente desde `raw` y
    deriva, por liga, los flags de cobertura y una heurística `xg_probable`.
  * `verify_xg` confirma con datos REALES (1 fetch /fixtures + 1 /fixtures/statistics)
    si una liga-temporada entrega `expected_goals`, porque el coverage promete pero
    no siempre cumple.

La heurística `xg_probable` NO garantiza xG: por eso existe `verify_xg`.
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from valuebet.db.models.raw import Payload
from valuebet.db.session import get_session
from valuebet.ingestion.normalize_catalog import SOURCE_CODE, _get_source_id
from valuebet.ingestion.normalize_fixtures import RESULT_BEARING_STATUSES, STATUS_MAP
from valuebet.ingestion.raw import ingestion_run

# Orden de columnas para tabla/CSV.
CSV_FIELDS = (
    "league_id",
    "name",
    "country",
    "season",
    "events",
    "lineups",
    "statistics_fixtures",
    "statistics_players",
    "standings",
    "odds",
    "xg_probable",
)


@dataclass
class LeagueCoverage:
    """Cobertura de una liga en su temporada más reciente disponible."""

    league_id: int
    name: str
    country: str | None
    season: int | None
    events: bool
    lineups: bool
    statistics_fixtures: bool
    statistics_players: bool
    standings: bool
    odds: bool

    @property
    def xg_probable(self) -> bool:
        """Heurística: coverage rico ⇒ xG probable (NO garantizado)."""
        return self.statistics_players and self.events and self.statistics_fixtures

    def as_row(self) -> dict:
        return {
            "league_id": self.league_id,
            "name": self.name,
            "country": self.country or "",
            "season": self.season if self.season is not None else "",
            "events": self.events,
            "lineups": self.lineups,
            "statistics_fixtures": self.statistics_fixtures,
            "statistics_players": self.statistics_players,
            "standings": self.standings,
            "odds": self.odds,
            "xg_probable": self.xg_probable,
        }


def _latest_leagues_payload(session: Session, source_id: uuid.UUID) -> dict | None:
    return session.execute(
        select(Payload.payload)
        .where(Payload.source_id == source_id, Payload.endpoint == "/leagues")
        .order_by(Payload.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def extract_coverage(payload: dict) -> list[LeagueCoverage]:
    """Proyecta el payload crudo de /leagues a filas de cobertura (tolerante a huecos)."""
    rows: list[LeagueCoverage] = []
    for item in payload.get("response", []):
        league = item.get("league", {})
        seasons = item.get("seasons", []) or []
        latest = max(seasons, key=lambda s: s.get("year", -1), default=None)
        coverage = (latest or {}).get("coverage", {}) or {}
        fixtures_cov = coverage.get("fixtures", {}) or {}
        rows.append(
            LeagueCoverage(
                league_id=league.get("id"),
                name=league.get("name", ""),
                country=(item.get("country") or {}).get("name"),
                season=(latest or {}).get("year"),
                events=bool(fixtures_cov.get("events")),
                lineups=bool(fixtures_cov.get("lineups")),
                statistics_fixtures=bool(fixtures_cov.get("statistics_fixtures")),
                statistics_players=bool(fixtures_cov.get("statistics_players")),
                standings=bool(coverage.get("standings")),
                odds=bool(coverage.get("odds")),
            )
        )
    return rows


def coverage_report(
    *,
    country: str | None = None,
    xg_probable: bool = False,
    season_min: int | None = None,
) -> list[LeagueCoverage]:
    """Lee el último /leagues de raw y devuelve la cobertura filtrada y ordenada."""
    with get_session() as session:
        source_id = _get_source_id(session)
        payload = _latest_leagues_payload(session, source_id)

    rows = extract_coverage(payload) if payload else []

    if country:
        needle = country.strip().lower()
        rows = [r for r in rows if r.country and r.country.lower() == needle]
    if season_min is not None:
        rows = [r for r in rows if r.season is not None and r.season >= season_min]
    if xg_probable:
        rows = [r for r in rows if r.xg_probable]

    # xg_probable desc, luego país, luego nombre.
    rows.sort(key=lambda r: (not r.xg_probable, (r.country or "").lower(), r.name.lower()))
    return rows


def export_csv(rows: list[LeagueCoverage], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_row())


# --------------------------------------------------------------------------- #
# verify-xg: confirmación REAL (consume ~2 peticiones)
# --------------------------------------------------------------------------- #
@dataclass
class VerifyXgResult:
    league_id: int
    season: int
    fixture_id: int | None
    xg_real: bool
    example_value: str | None = None
    note: str | None = None


def _first_terminal_fixture(payload: dict) -> int | None:
    for item in payload.get("response", []):
        short = (item.get("fixture", {}).get("status") or {}).get("short")
        if STATUS_MAP.get(short) in RESULT_BEARING_STATUSES:
            return item.get("fixture", {}).get("id")
    return None


def _find_expected_goals(payload: dict) -> str | None:
    for entry in payload.get("response", []):
        for stat in entry.get("statistics", []) or []:
            if stat.get("type") == "expected_goals" and stat.get("value") not in (None, ""):
                return str(stat.get("value"))
    return None


def verify_xg(adapter, league_id: int, season: int, *, sessionmaker_=None) -> VerifyXgResult:
    """Confirma con datos REALES si una liga-temporada entrega xG (~2 peticiones).

    Trae /fixtures, toma el primer partido terminal, trae sus /fixtures/statistics y
    reporta si `expected_goals` viene poblado. Persiste ambos payloads en raw.
    """
    result = VerifyXgResult(league_id=league_id, season=season, fixture_id=None, xg_real=False)
    with ingestion_run(
        SOURCE_CODE,
        "verify_xg",
        params={"league": league_id, "season": season},
        sessionmaker_=sessionmaker_,
    ) as run:
        fixtures_res = adapter.fetch_fixtures(league_id, season)
        run.persist(fixtures_res)

        fixture_id = _first_terminal_fixture(fixtures_res.payload)
        result.fixture_id = fixture_id
        if fixture_id is None:
            result.note = "no se halló un partido terminal en la muestra"
        else:
            stats_res = adapter.fetch_fixture_statistics(fixture_id)
            run.persist(stats_res)
            value = _find_expected_goals(stats_res.payload)
            result.xg_real = value is not None
            result.example_value = value

    return result
