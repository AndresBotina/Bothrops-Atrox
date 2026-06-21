"""Interfaz de modelo y tipos de dominio del evaluador (Fase 2, HU 2.1).

Este módulo define el CONTRATO que cualquier modelo predictivo debe cumplir para
ser evaluado por el walk-forward. La pieza clave es el `Protocol` `PredictionModel`:
el evaluador (`backtest.py`) sólo conoce esta interfaz, de modo que un modelo
futuro (Dixon-Coles, Fase 3) se enchufa implementándola, sin reescribir el
evaluador ni las métricas.

Tipos puros, sin dependencias de la base de datos: la capa de datos
(`evaluation/data.py`) traduce `core.matches` → `Match` y el evaluador opera
sólo sobre estos objetos.

Contrato de NO-LOOKAHEAD (invariante #1 del CLAUDE.md):
  * `fit(matches)` recibe SÓLO partidos cuyo `kickoff_utc` es ANTERIOR al saque
    del partido que se va a predecir. El evaluador lo garantiza por construcción.
  * `predict_proba(match)` NO debe leer los campos de resultado del partido
    (`home_goals`/`away_goals`/`outcome`): son el "futuro" respecto al saque.
    El modelo sólo puede usar identidad de equipos, fecha y demás info pre-saque.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

# Resultado 1X2 desde la perspectiva del partido (no del equipo).
Outcome = Literal["home", "draw", "away"]
OUTCOMES: tuple[Outcome, ...] = ("home", "draw", "away")

# Tolerancia al validar que una distribución suma 1.0. Cubre tanto el error de
# coma flotante como el redondeo a 5 decimales con que se PERSISTEN las
# probabilidades (Numeric(6,5)): tres valores redondeados pueden sumar 0.99999.
# Sigue siendo lo bastante estricta para detectar distribuciones mal formadas.
_SUM_TOL = 1e-4


@dataclass(frozen=True, slots=True)
class Probabilities:
    """Distribución 1X2 inmutable. Garantiza por construcción que suma 1.0."""

    home: float
    draw: float
    away: float

    def __post_init__(self) -> None:
        for name, p in (("home", self.home), ("draw", self.draw), ("away", self.away)):
            if not (-_SUM_TOL <= p <= 1.0 + _SUM_TOL):
                raise ValueError(f"probabilidad '{name}' fuera de [0,1]: {p}")
        total = self.home + self.draw + self.away
        if abs(total - 1.0) > _SUM_TOL:
            raise ValueError(f"las probabilidades 1X2 deben sumar 1.0, suman {total}")

    def get(self, outcome: Outcome) -> float:
        """Probabilidad asignada a un desenlace concreto."""
        return getattr(self, outcome)

    def as_dict(self) -> dict[str, float]:
        return {"home": self.home, "draw": self.draw, "away": self.away}

    @classmethod
    def uniform(cls) -> Probabilities:
        """Distribución sin información: 1/3 a cada desenlace."""
        third = 1.0 / 3.0
        return cls(third, third, third)


@dataclass(frozen=True, slots=True)
class Match:
    """Vista de dominio de un partido, desacoplada del ORM.

    Los campos de resultado son opcionales: un partido aún no jugado los tiene en
    `None`. `outcome`/`is_finished` derivan el desenlace 1X2 cuando hay resultado.
    El modelo NO debe leer los goles en `predict_proba` (serían lookahead).
    """

    match_id: uuid.UUID
    kickoff_utc: datetime
    home_team_id: uuid.UUID
    away_team_id: uuid.UUID
    home_goals: int | None = None
    away_goals: int | None = None

    @property
    def is_finished(self) -> bool:
        return self.home_goals is not None and self.away_goals is not None

    @property
    def outcome(self) -> Outcome | None:
        """Desenlace 1X2 real, o `None` si el partido no tiene resultado."""
        if self.home_goals is None or self.away_goals is None:
            return None
        if self.home_goals > self.away_goals:
            return "home"
        if self.home_goals < self.away_goals:
            return "away"
        return "draw"


@runtime_checkable
class PredictionModel(Protocol):
    """Contrato de cualquier modelo evaluable por el walk-forward.

    Cualquier modelo futuro (Dixon-Coles) implementa estos dos métodos y queda
    enchufado al evaluador sin tocar `backtest.py` ni `metrics.py`.
    """

    def fit(self, matches: Sequence[Match]) -> None:
        """Entrena con un conjunto de partidos PASADOS (todos anteriores al saque
        del partido a predecir). El evaluador garantiza ese filtro temporal."""
        ...

    def predict_proba(self, match: Match) -> Probabilities:
        """Devuelve la distribución 1X2 del partido SIN mirar su resultado."""
        ...
