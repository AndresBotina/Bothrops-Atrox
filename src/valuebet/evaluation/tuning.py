"""Selección del hiperparámetro de ponderación temporal (HU 3.3).

`half_life` (días) NO se estima por máxima verosimilitud: las verosimilitudes
ponderadas con distinto ξ no son comparables. Es un HIPERPARÁMETRO que se elige
empíricamente, barriendo una rejilla y comparando el Brier/log loss del
walk-forward.

⚠️ RIESGO DE LOOKAHEAD EN LA SELECCIÓN: elegir el half_life que minimiza el Brier
sobre TODO el histórico y luego reportar ESE mismo Brier es una forma sutil de
sobreajuste (se "mira el futuro" al elegir). Para esta HU reportamos la tabla
completa para inspección; la selección rigurosa exige un periodo de validación
separado del de reporte (out-of-sample), que se puede refinar más adelante.
"""

from __future__ import annotations

from dataclasses import dataclass

from valuebet.evaluation.backtest import walk_forward
from valuebet.evaluation.baselines import build_model
from valuebet.evaluation.metrics import BacktestMetrics, evaluate
from valuebet.evaluation.model import Match

# Rejilla por defecto de half-lives en DÍAS; None = infinito (sin decaimiento).
DEFAULT_HALF_LIFE_GRID: tuple[float | None, ...] = (90, 180, 365, 540, 730, None)


@dataclass(frozen=True, slots=True)
class SweepRow:
    """Resultado del walk-forward para un half_life concreto."""

    half_life: float | None  # None = infinito (sin decaimiento, equivale a HU 3.2)
    metrics: BacktestMetrics


def sweep_half_lives(
    model_name: str,
    matches: list[Match],
    half_lives: tuple[float | None, ...] = DEFAULT_HALF_LIFE_GRID,
    *,
    train_window: int | None = None,
    step: int = 1,
    min_train: int = 20,
    n_bins: int = 10,
) -> list[SweepRow]:
    """Corre el walk-forward para cada half_life y devuelve sus métricas.

    El mismo conjunto de partidos se reusa en todas las configuraciones, así que
    las filas son comparables entre sí.
    """
    rows: list[SweepRow] = []
    for hl in half_lives:
        model = build_model(model_name, half_life=hl)
        result = walk_forward(
            model, matches, train_window=train_window, step=step, min_train=min_train
        )
        if not result.records:
            continue
        rows.append(SweepRow(half_life=hl, metrics=evaluate(result.records, n_bins=n_bins)))
    return rows


def best_by_brier(rows: list[SweepRow]) -> SweepRow:
    """Fila con menor Brier. Lanza ValueError si no hay filas."""
    if not rows:
        raise ValueError("no hay filas en el barrido")
    return min(rows, key=lambda r: r.metrics.brier)
