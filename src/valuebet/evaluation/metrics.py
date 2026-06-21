"""Métricas de calidad y CALIBRACIÓN de predicciones 1X2 (HU 2.1).

El norte del proyecto NO es el % de acierto sino la CALIBRACIÓN: cuando el
modelo dice 60%, ¿ocurre el 60%? Por eso las métricas objetivo son el Brier
score y el log loss (penalizan la sobreconfianza), más la tabla de fiabilidad.
La `accuracy` se incluye SÓLO como referencia humana, nunca como objetivo de
optimización (ver §1 del CLAUDE.md).

Todo es Python puro (sin numpy): el paquete base no depende de numpy y estas
cuentas son triviales.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from statistics import mean

from valuebet.evaluation.model import OUTCOMES, Outcome, Probabilities

# Recorte para el log loss: evita log(0) = -inf cuando el modelo asigna prob 0
# al desenlace que ocurrió.
_LOG_EPS = 1e-15


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """Una predicción evaluada: probabilidades emitidas vs desenlace real."""

    match_id: uuid.UUID
    kickoff_utc: datetime
    probs: Probabilities
    outcome: Outcome


# --------------------------------------------------------------------------- #
# Métricas por predicción
# --------------------------------------------------------------------------- #
def brier_score(probs: Probabilities, outcome: Outcome) -> float:
    """Brier multiclase: suma de (p_k - y_k)^2 sobre los 3 desenlaces.

    y_k es one-hot (1 en el desenlace real). Rango [0, 2]; 0 = predicción
    perfecta. Menor es mejor.
    """
    return sum((probs.get(o) - (1.0 if o == outcome else 0.0)) ** 2 for o in OUTCOMES)


def log_loss(probs: Probabilities, outcome: Outcome, *, eps: float = _LOG_EPS) -> float:
    """Log loss multiclase de una predicción: -ln(p(desenlace_real)).

    La probabilidad se recorta a [eps, 1] para evitar -inf. Menor es mejor.
    """
    p = min(max(probs.get(outcome), eps), 1.0)
    return -math.log(p)


def _argmax_outcome(probs: Probabilities) -> Outcome:
    """Desenlace más probable; desempata en el orden home, draw, away."""
    best: Outcome = OUTCOMES[0]
    best_p = probs.get(best)
    for o in OUTCOMES[1:]:
        if probs.get(o) > best_p:
            best, best_p = o, probs.get(o)
    return best


# --------------------------------------------------------------------------- #
# Métricas agregadas
# --------------------------------------------------------------------------- #
def mean_brier(records: list[PredictionRecord]) -> float:
    _require(records)
    return mean(brier_score(r.probs, r.outcome) for r in records)


def mean_log_loss(records: list[PredictionRecord]) -> float:
    _require(records)
    return mean(log_loss(r.probs, r.outcome) for r in records)


def accuracy(records: list[PredictionRecord]) -> float:
    """Fracción de aciertos del desenlace más probable.

    REFERENCIA, NO objetivo: un modelo puede ser muy acertado y estar mal
    calibrado (sobreconfiado). Optimizar accuracy está prohibido (§1).
    """
    _require(records)
    hits = sum(1 for r in records if _argmax_outcome(r.probs) == r.outcome)
    return hits / len(records)


# --------------------------------------------------------------------------- #
# Curva de calibración / tabla de fiabilidad
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """Un tramo de probabilidad de la tabla de fiabilidad.

    Agrupa puntos (uno por clase y predicción, esquema one-vs-rest) cuya
    probabilidad predicha cae en [lower, upper). `mean_predicted` es la media de
    esas probabilidades; `observed_freq` es la frecuencia real con que ocurrió la
    clase. Calibrado ⇔ mean_predicted ≈ observed_freq.
    """

    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_freq: float


def calibration_table(records: list[PredictionRecord], *, n_bins: int = 10) -> list[CalibrationBin]:
    """Tabla de fiabilidad multiclase (one-vs-rest agrupado).

    Por cada predicción se generan 3 puntos (una por desenlace): la probabilidad
    predicha de esa clase y si la clase ocurrió (1/0). Los puntos se reparten en
    `n_bins` tramos iguales de [0, 1] y se compara, por tramo, la probabilidad
    media predicha contra la frecuencia observada. Devuelve los `n_bins` tramos
    (los vacíos con count=0).
    """
    if n_bins < 1:
        raise ValueError("n_bins debe ser >= 1")

    sums = [0.0] * n_bins  # suma de probabilidades predichas por tramo
    hits = [0.0] * n_bins  # suma de aciertos (clase ocurrió) por tramo
    counts = [0] * n_bins

    for r in records:
        for o in OUTCOMES:
            p = r.probs.get(o)
            idx = min(int(p * n_bins), n_bins - 1)  # p == 1.0 cae en el último
            sums[idx] += p
            hits[idx] += 1.0 if r.outcome == o else 0.0
            counts[idx] += 1

    table: list[CalibrationBin] = []
    for i in range(n_bins):
        c = counts[i]
        table.append(
            CalibrationBin(
                lower=i / n_bins,
                upper=(i + 1) / n_bins,
                count=c,
                mean_predicted=(sums[i] / c) if c else 0.0,
                observed_freq=(hits[i] / c) if c else 0.0,
            )
        )
    return table


def expected_calibration_error(records: list[PredictionRecord], *, n_bins: int = 10) -> float:
    """ECE: error de calibración medio, ponderado por nº de puntos por tramo.

    Promedio de |mean_predicted - observed_freq| pesado por la población de cada
    tramo. 0 = perfectamente calibrado. Resume la tabla de fiabilidad en un
    número.
    """
    table = calibration_table(records, n_bins=n_bins)
    total = sum(b.count for b in table)
    if total == 0:
        return 0.0
    return sum(b.count * abs(b.mean_predicted - b.observed_freq) for b in table) / total


# --------------------------------------------------------------------------- #
# Resumen
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Conjunto de métricas de un backtest, listo para reportar/persistir."""

    n: int
    brier: float
    log_loss: float
    accuracy: float
    ece: float
    calibration: list[CalibrationBin]

    def as_summary(self) -> dict:
        """Resumen serializable (sin la tabla de tramos)."""
        return {
            "n": self.n,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "accuracy": self.accuracy,
            "ece": self.ece,
        }


def evaluate(records: list[PredictionRecord], *, n_bins: int = 10) -> BacktestMetrics:
    """Calcula todas las métricas de un conjunto de predicciones evaluadas."""
    _require(records)
    return BacktestMetrics(
        n=len(records),
        brier=mean_brier(records),
        log_loss=mean_log_loss(records),
        accuracy=accuracy(records),
        ece=expected_calibration_error(records, n_bins=n_bins),
        calibration=calibration_table(records, n_bins=n_bins),
    )


def _require(records: list[PredictionRecord]) -> None:
    if not records:
        raise ValueError("no hay predicciones que evaluar (lista vacía)")
