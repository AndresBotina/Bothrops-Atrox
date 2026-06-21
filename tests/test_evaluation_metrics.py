"""HU 2.1 — Métricas de calibración: cálculo a mano, casos límite y tabla."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime

import pytest

from valuebet.evaluation.metrics import (
    PredictionRecord,
    accuracy,
    brier_score,
    calibration_table,
    evaluate,
    expected_calibration_error,
    log_loss,
    mean_brier,
    mean_log_loss,
)
from valuebet.evaluation.model import Outcome, Probabilities

_KICKOFF = datetime(2023, 8, 1, 14, 0, tzinfo=UTC)


def _rec(probs: Probabilities, outcome: Outcome) -> PredictionRecord:
    return PredictionRecord(uuid.uuid4(), _KICKOFF, probs, outcome)


# --- (b) Brier y log loss calculados a mano ---------------------------------
def test_brier_matches_hand_calculation() -> None:
    probs = Probabilities(0.6, 0.3, 0.1)
    # outcome home: (0.6-1)^2 + (0.3-0)^2 + (0.1-0)^2 = 0.16 + 0.09 + 0.01
    assert brier_score(probs, "home") == pytest.approx(0.26)
    # outcome draw: 0.36 + 0.49 + 0.01
    assert brier_score(probs, "draw") == pytest.approx(0.86)


def test_log_loss_matches_hand_calculation() -> None:
    probs = Probabilities(0.6, 0.3, 0.1)
    assert log_loss(probs, "home") == pytest.approx(-math.log(0.6))
    assert log_loss(probs, "away") == pytest.approx(-math.log(0.1))


# --- (c) modelo perfecto vs aleatorio ---------------------------------------
def test_perfect_model_scores_zero() -> None:
    records = [_rec(Probabilities(1.0, 0.0, 0.0), "home")]
    assert mean_brier(records) == pytest.approx(0.0)
    assert mean_log_loss(records) == pytest.approx(0.0)
    assert accuracy(records) == pytest.approx(1.0)


def test_uniform_model_scores_known_values() -> None:
    # Distribución uniforme: Brier = 6/9, log loss = ln(3), sea cual sea el real.
    records = [_rec(Probabilities.uniform(), "away")]
    assert mean_brier(records) == pytest.approx(6.0 / 9.0)
    assert mean_log_loss(records) == pytest.approx(math.log(3.0))


def test_perfect_model_beats_uniform() -> None:
    outcome: Outcome = "draw"
    perfect = [_rec(Probabilities(0.0, 1.0, 0.0), outcome)]
    uniform = [_rec(Probabilities.uniform(), outcome)]
    assert mean_brier(perfect) < mean_brier(uniform)
    assert mean_log_loss(perfect) < mean_log_loss(uniform)


# --- (d) tabla de calibración: agrupa y cuenta bien -------------------------
def test_calibration_table_groups_and_counts() -> None:
    # 10 predicciones idénticas (0.5, 0.25, 0.25), todas con resultado 'home'.
    records = [_rec(Probabilities(0.5, 0.25, 0.25), "home") for _ in range(10)]
    table = calibration_table(records, n_bins=10)
    assert len(table) == 10

    # home (0.5) cae en el tramo [0.5,0.6) → índice 5: 10 puntos, todos aciertan.
    b5 = table[5]
    assert (b5.lower, b5.upper) == (0.5, 0.6)
    assert b5.count == 10
    assert b5.mean_predicted == pytest.approx(0.5)
    assert b5.observed_freq == pytest.approx(1.0)

    # draw y away (0.25) caen en [0.2,0.3) → índice 2: 20 puntos, 0 aciertos.
    b2 = table[2]
    assert b2.count == 20
    assert b2.mean_predicted == pytest.approx(0.25)
    assert b2.observed_freq == pytest.approx(0.0)

    # Los tramos vacíos existen con conteo 0.
    assert table[0].count == 0


def test_perfectly_calibrated_set_has_zero_ece() -> None:
    # 100 predicciones a 0.5 de 'home'; exactamente la mitad ocurre.
    records = [_rec(Probabilities(0.5, 0.3, 0.2), "home" if i < 50 else "draw") for i in range(100)]
    table = {(b.lower, b.upper): b for b in calibration_table(records, n_bins=10)}
    b5 = table[(0.5, 0.6)]
    assert b5.observed_freq == pytest.approx(0.5)
    assert b5.mean_predicted == pytest.approx(0.5)


def test_evaluate_bundles_metrics() -> None:
    records = [_rec(Probabilities(0.6, 0.3, 0.1), "home") for _ in range(5)]
    m = evaluate(records, n_bins=10)
    assert m.n == 5
    assert m.brier == pytest.approx(0.26)
    assert m.accuracy == pytest.approx(1.0)
    assert m.ece == pytest.approx(expected_calibration_error(records, n_bins=10))


def test_empty_records_raise() -> None:
    with pytest.raises(ValueError):
        evaluate([])
