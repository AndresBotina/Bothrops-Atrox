"""Evaluador WALK-FORWARD: el núcleo de la Fase 2 (HU 2.1).

Recorre los partidos en orden cronológico y, para cada uno, entrena el modelo
SÓLO con partidos anteriores a su saque y luego predice. Es función pura: opera
sobre objetos `Match` de dominio, sin tocar la base de datos.

CÓMO SE EVITA EL LOOKAHEAD (invariante #1), POR CONSTRUCCIÓN
------------------------------------------------------------
Para predecir un partido con saque en T, el conjunto de entrenamiento se obtiene
con `bisect_left` sobre la lista de saques ORDENADA:

    cut = bisect_left(kickoffs, T)      # nº de partidos con saque ESTRICTAMENTE < T
    train_pool = finished[:cut]

`bisect_left` deja fuera cualquier partido con saque == T (simultáneos del mismo
horario: su resultado tampoco se conoce antes del saque) y, obviamente, los
posteriores. No hay forma de que un dato con fecha >= T entre al entrenamiento:
el filtro es temporal y estricto, no "por disciplina". El test `test_no_lookahead`
lo verifica espiando exactamente qué recibió `fit` antes de cada predicción.

Re-entrenamiento por lotes (`step`): re-ajustar el modelo en cada partido es
caro. Con `step > 1` se reutiliza el último ajuste para los siguientes `step`
partidos. Eso NO introduce lookahead: un ajuste reutilizado se hizo con un corte
temporal AÚN MÁS antiguo, luego sigue siendo estrictamente anterior al saque
actual.

`min_train`: si el historial disponible es menor que `min_train`, NO se predice
ese partido (no hay base suficiente). Así, los primeros partidos del histórico
no reciben predicción (criterio (e) de la HU).
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass

from valuebet.evaluation.metrics import PredictionRecord
from valuebet.evaluation.model import Match, PredictionModel


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Resultado del walk-forward: predicciones y conteos de cobertura."""

    records: list[PredictionRecord]
    n_predicted: int
    n_skipped: int  # partidos sin historial suficiente (no predichos)
    n_finished: int  # partidos con resultado disponibles en total
    config: dict


def walk_forward(
    model: PredictionModel,
    matches: Sequence[Match],
    *,
    train_window: int | None = None,
    step: int = 1,
    min_train: int = 1,
) -> BacktestResult:
    """Corre el walk-forward de `model` sobre `matches`.

    Args:
        model: cualquier implementación de `PredictionModel`.
        matches: partidos (se filtran a los que tienen resultado y se ordenan
            por saque internamente).
        train_window: si se da, ventana DESLIZANTE de los últimos N partidos
            previos; si es `None`, ventana EXPANSIVA (todo el pasado).
        step: cada cuántas predicciones se re-entrena el modelo (>= 1).
        min_train: mínimo de partidos de entrenamiento para emitir predicción.

    Returns:
        `BacktestResult` con una `PredictionRecord` por partido evaluable.
    """
    if step < 1:
        raise ValueError("step debe ser >= 1")
    if min_train < 1:
        raise ValueError("min_train debe ser >= 1")
    if train_window is not None and train_window < 1:
        raise ValueError("train_window debe ser >= 1 o None")

    # Sólo partidos con resultado; orden cronológico estable (saque, luego id).
    finished = sorted(
        (m for m in matches if m.is_finished),
        key=lambda m: (m.kickoff_utc, str(m.match_id)),
    )
    kickoffs = [m.kickoff_utc for m in finished]

    records: list[PredictionRecord] = []
    skipped = 0
    preds_since_fit = step  # fuerza el primer ajuste

    for target in finished:
        # Partidos con saque ESTRICTAMENTE anterior al de `target`.
        cut = bisect.bisect_left(kickoffs, target.kickoff_utc)
        train_pool = finished[:cut]

        if len(train_pool) < min_train:
            skipped += 1
            continue

        if preds_since_fit >= step:
            window = train_pool[-train_window:] if train_window else train_pool
            model.fit(window)
            preds_since_fit = 0

        probs = model.predict_proba(target)
        outcome = target.outcome
        assert outcome is not None  # garantizado por is_finished
        records.append(
            PredictionRecord(
                match_id=target.match_id,
                kickoff_utc=target.kickoff_utc,
                probs=probs,
                outcome=outcome,
            )
        )
        preds_since_fit += 1

    return BacktestResult(
        records=records,
        n_predicted=len(records),
        n_skipped=skipped,
        n_finished=len(finished),
        config={
            "train_window": train_window,
            "step": step,
            "min_train": min_train,
        },
    )
