"""Persistencia de backtests en `models` y relectura para revisión (HU 2.1).

DECISIÓN DE DISEÑO (documentada, como pide la HU)
-------------------------------------------------
Se reutiliza el esquema existente, SIN migración nueva:

  * `models.model_versions`: una fila por CORRIDA de backtest. Identidad =
    (name, version). Guarda algoritmo, hiperparámetros (config del walk-forward +
    liga), rango temporal de entrenamiento, hash de código y `notes` con un
    resumen humano de métricas. Es el "registro" de la evaluación.
  * `models.predictions`: una fila por (partido, selección) — 3 por partido
    (home/draw/away) en el mercado '1x2'. Es la MATERIA PRIMA: a partir de ella,
    cruzada con el resultado real en `core.matches`, se RECALCULAN las métricas
    (Brier, log loss, calibración). No se persisten métricas derivadas como
    valores autónomos (evitamos duplicar datos derivables); `notes` lleva sólo
    una copia informativa para lectura rápida.

Idempotencia (invariante #5): re-correr un backtest con la misma (name, version)
reusa la fila de `model_versions` y REEMPLAZA sus predicciones (delete + insert),
nunca duplica. Se evita así también el problema de `UNIQUE` con `line = NULL`.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from valuebet.db.models.core import Match as MatchModel
from valuebet.db.models.models import ModelVersion, Prediction
from valuebet.db.session import get_sessionmaker
from valuebet.evaluation.backtest import BacktestResult
from valuebet.evaluation.metrics import BacktestMetrics, PredictionRecord
from valuebet.evaluation.model import OUTCOMES, Outcome, Probabilities

MARKET_1X2 = "1x2"


def _code_hash() -> str | None:
    """Hash corto del commit actual para reproducibilidad; `None` si no hay git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _get_or_create_model_version(
    session: Session, *, name: str, version: str, algorithm: str
) -> ModelVersion:
    mv = session.execute(
        select(ModelVersion).where(ModelVersion.name == name, ModelVersion.version == version)
    ).scalar_one_or_none()
    if mv is None:
        mv = ModelVersion(name=name, version=version, algorithm=algorithm)
        session.add(mv)
        session.flush()
    return mv


def persist_backtest(
    result: BacktestResult,
    metrics: BacktestMetrics,
    *,
    league_external_id: int,
    name: str,
    version: str,
    algorithm: str,
    from_season: int | None = None,
    sessionmaker_: sessionmaker[Session] | None = None,
) -> uuid.UUID:
    """Persiste un backtest y devuelve el id de la `model_versions` resultante."""
    sm = sessionmaker_ or get_sessionmaker()
    session = sm()
    try:
        mv = _get_or_create_model_version(session, name=name, version=version, algorithm=algorithm)

        # Rango temporal de las predicciones (proxy del rango entrenado/evaluado).
        kickoffs = [r.kickoff_utc for r in result.records]
        mv.algorithm = algorithm
        mv.hyperparameters = {
            "league": league_external_id,
            "from_season": from_season,
            "n_predicted": result.n_predicted,
            "n_skipped": result.n_skipped,
            "n_finished": result.n_finished,
            **result.config,
        }
        mv.train_data_from = min(kickoffs).date() if kickoffs else None
        mv.train_data_to = max(kickoffs).date() if kickoffs else None
        mv.code_hash = _code_hash()
        mv.trained_at = datetime.now(UTC)
        mv.notes = (
            f"backtest {algorithm}: n={metrics.n} brier={metrics.brier:.4f} "
            f"log_loss={metrics.log_loss:.4f} accuracy={metrics.accuracy:.4f} "
            f"ece={metrics.ece:.4f}"
        )
        session.flush()

        # Idempotencia: borra las predicciones previas de esta versión y reinserta.
        session.execute(delete(Prediction).where(Prediction.model_version_id == mv.id))
        for record in result.records:
            for outcome in OUTCOMES:
                session.add(
                    Prediction(
                        match_id=record.match_id,
                        model_version_id=mv.id,
                        market_type=MARKET_1X2,
                        selection_code=outcome,
                        line=None,
                        predicted_prob=Decimal(f"{record.probs.get(outcome):.5f}"),
                    )
                )
        session.flush()
        mv_id = mv.id
        session.commit()
        return mv_id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass(frozen=True, slots=True)
class _Selection:
    selection_code: str
    predicted_prob: float


def load_backtest_records(
    model_version_id: uuid.UUID,
    *,
    sessionmaker_: sessionmaker[Session] | None = None,
) -> list[PredictionRecord]:
    """Reconstruye las `PredictionRecord` de un backtest persistido.

    Cruza `models.predictions` con el resultado real en `core.matches` para
    recuperar el desenlace. Permite recalcular métricas sin re-correr el modelo:
    demuestra que la persistencia es revisable y cierra el ciclo guardar→leer.
    """
    sm = sessionmaker_ or get_sessionmaker()
    session = sm()
    try:
        rows = session.execute(
            select(
                Prediction.match_id,
                Prediction.selection_code,
                Prediction.predicted_prob,
                MatchModel.kickoff_utc,
                MatchModel.home_goals,
                MatchModel.away_goals,
            )
            .join(MatchModel, MatchModel.id == Prediction.match_id)
            .where(
                Prediction.model_version_id == model_version_id,
                Prediction.market_type == MARKET_1X2,
            )
        ).all()
    finally:
        session.close()

    # Agrupa las 3 selecciones por partido.
    by_match: dict[uuid.UUID, dict] = {}
    for match_id, selection, prob, kickoff, hg, ag in rows:
        entry = by_match.setdefault(match_id, {"kickoff": kickoff, "hg": hg, "ag": ag, "probs": {}})
        entry["probs"][selection] = float(prob)

    records: list[PredictionRecord] = []
    for match_id, entry in by_match.items():
        probs = Probabilities(
            home=entry["probs"]["home"],
            draw=entry["probs"]["draw"],
            away=entry["probs"]["away"],
        )
        outcome = _outcome_from_goals(entry["hg"], entry["ag"])
        records.append(
            PredictionRecord(
                match_id=match_id,
                kickoff_utc=entry["kickoff"],
                probs=probs,
                outcome=outcome,
            )
        )
    records.sort(key=lambda r: (r.kickoff_utc, str(r.match_id)))
    return records


def _outcome_from_goals(home_goals: int, away_goals: int) -> Outcome:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"
