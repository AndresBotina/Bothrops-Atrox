"""HU 2.1 — Integración: load_matches desde core, persistencia y relectura.

Datos sembrados en la base de TEST (sin API real). Verifica el ciclo completo:
core → load_matches → walk_forward → persist_backtest → load_backtest_records,
y que las métricas recalculadas desde lo persistido coinciden con las originales.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from valuebet.adapters.sources import seed_sources
from valuebet.db.models.core import (
    Competition,
    Match,
    Season,
    SourceEntityMap,
    Sport,
    Team,
)
from valuebet.db.models.meta import Source
from valuebet.db.models.models import ModelVersion, Prediction
from valuebet.db.session import get_session
from valuebet.evaluation.backtest import walk_forward
from valuebet.evaluation.baselines import HomeAdvantageBaseline
from valuebet.evaluation.data import load_matches
from valuebet.evaluation.metrics import evaluate
from valuebet.evaluation.store import load_backtest_records, persist_backtest

pytestmark = pytest.mark.integration

_TRUNCATE = text(
    "TRUNCATE models.predictions, models.model_versions, "
    "core.match_team_stats, core.matches, core.source_entity_map, "
    "core.team_aliases, core.team_season, core.player_team, core.players, "
    "core.seasons, core.competitions, core.teams, core.venues, core.countries, "
    "core.sports, raw.payloads, meta.data_quality_checks, meta.ingestion_runs, "
    "meta.sources RESTART IDENTITY CASCADE"
)

_T0 = datetime(2022, 8, 1, 14, 0, tzinfo=UTC)
LEAGUE_EXTERNAL_ID = 39


@pytest.fixture
def seeded(migrated_db):
    with get_session() as session:
        session.execute(_TRUNCATE)
    with get_session() as session:
        seed_sources(session)
    yield


def _seed_league(n_matches: int = 30) -> int:
    """Siembra una liga con n partidos terminales. Devuelve cuántos creó."""
    with get_session() as session:
        source_id = session.execute(
            select(Source.id).where(Source.code == "api_sports")
        ).scalar_one()

        sport = Sport(code="football", name="Football")
        session.add(sport)
        session.flush()
        comp = Competition(sport_id=sport.id, name="Premier League", kind="league")
        session.add(comp)
        session.flush()
        # Mapea el id externo de la liga (39) → competición interna.
        session.add(
            SourceEntityMap(
                source_id=source_id,
                entity_type="competition",
                external_id=str(LEAGUE_EXTERNAL_ID),
                internal_id=comp.id,
            )
        )
        season = Season(competition_id=comp.id, label="2022")
        session.add(season)
        session.flush()

        teams = []
        for name in ("A", "B", "C", "D"):
            t = Team(sport_id=sport.id, name=f"Team {name}")
            session.add(t)
            session.flush()
            teams.append(t)

        # Resultados variados (locales, empates, visitantes) para métricas no triviales.
        results = [(2, 0), (1, 1), (0, 1)]
        for i in range(n_matches):
            hg, ag = results[i % 3]
            home = teams[i % len(teams)]
            away = teams[(i + 1) % len(teams)]
            session.add(
                Match(
                    season_id=season.id,
                    home_team_id=home.id,
                    away_team_id=away.id,
                    status_code="finished",
                    kickoff_utc=_T0 + timedelta(days=i),
                    home_goals=hg,
                    away_goals=ag,
                )
            )
        session.flush()
    return n_matches


def test_load_matches_resolves_league_and_filters(seeded) -> None:
    _seed_league(12)
    matches = load_matches(LEAGUE_EXTERNAL_ID)
    assert len(matches) == 12
    assert all(m.is_finished for m in matches)
    # Orden cronológico ascendente.
    kickoffs = [m.kickoff_utc for m in matches]
    assert kickoffs == sorted(kickoffs)


def test_load_matches_unknown_league_raises(seeded) -> None:
    _seed_league(3)
    with pytest.raises(LookupError):
        load_matches(99999)


def test_persist_and_reload_roundtrip(seeded) -> None:
    _seed_league(30)
    matches = load_matches(LEAGUE_EXTERNAL_ID)
    result = walk_forward(HomeAdvantageBaseline(), matches, step=1, min_train=5)
    metrics = evaluate(result.records)

    mv_id = persist_backtest(
        result,
        metrics,
        league_external_id=LEAGUE_EXTERNAL_ID,
        name="baseline:home-advantage",
        version="test-v1",
        algorithm="home-advantage",
    )

    # 3 predicciones (home/draw/away) por partido evaluado.
    with get_session() as session:
        n_preds = session.scalar(
            select(func.count()).select_from(Prediction).where(Prediction.model_version_id == mv_id)
        )
        assert n_preds == result.n_predicted * 3
        mv = session.get(ModelVersion, mv_id)
        assert mv.hyperparameters["league"] == LEAGUE_EXTERNAL_ID
        assert mv.hyperparameters["n_predicted"] == result.n_predicted

    # Recalcular métricas desde lo persistido coincide con las originales.
    reloaded = load_backtest_records(mv_id)
    assert len(reloaded) == result.n_predicted
    reloaded_metrics = evaluate(reloaded)
    assert reloaded_metrics.brier == pytest.approx(metrics.brier, abs=1e-4)
    assert reloaded_metrics.log_loss == pytest.approx(metrics.log_loss, abs=1e-4)
    assert reloaded_metrics.accuracy == pytest.approx(metrics.accuracy)


def test_persist_is_idempotent(seeded) -> None:
    _seed_league(30)
    matches = load_matches(LEAGUE_EXTERNAL_ID)
    result = walk_forward(HomeAdvantageBaseline(), matches, step=1, min_train=5)
    metrics = evaluate(result.records)

    kwargs = {
        "league_external_id": LEAGUE_EXTERNAL_ID,
        "name": "baseline:home-advantage",
        "version": "test-v1",
        "algorithm": "home-advantage",
    }
    mv_id1 = persist_backtest(result, metrics, **kwargs)
    mv_id2 = persist_backtest(result, metrics, **kwargs)

    assert mv_id1 == mv_id2  # misma (name, version) → misma fila
    with get_session() as session:
        n_versions = session.scalar(select(func.count()).select_from(ModelVersion))
        n_preds = session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.model_version_id == mv_id1)
        )
        assert n_versions == 1  # no se duplica la versión
        assert n_preds == result.n_predicted * 3  # ni las predicciones
