"""Capa evaluation de valuebet: evaluador walk-forward y métricas (Fase 2).

El evaluador se construye ANTES que el modelo (invariante #9): ningún modelo se
da por válido hasta que el backtest walk-forward lo mide. En esta fase NO se
miden ROI ni CLV (requieren cuotas, Fase 4): sólo CALIDAD/CALIBRACIÓN de la
predicción (Brier, log loss, tabla de fiabilidad).
"""

from __future__ import annotations

from valuebet.evaluation.backtest import BacktestResult, walk_forward
from valuebet.evaluation.baselines import (
    HomeAdvantageBaseline,
    TeamFrequencyBaseline,
    build_model,
)
from valuebet.evaluation.metrics import (
    BacktestMetrics,
    PredictionRecord,
    calibration_table,
    evaluate,
)
from valuebet.evaluation.model import Match, PredictionModel, Probabilities

__all__ = [
    "BacktestMetrics",
    "BacktestResult",
    "HomeAdvantageBaseline",
    "Match",
    "PredictionModel",
    "PredictionRecord",
    "Probabilities",
    "TeamFrequencyBaseline",
    "build_model",
    "calibration_table",
    "evaluate",
    "walk_forward",
]
