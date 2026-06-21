"""Modelo POISSON básico de goles (HU 3.1) — primera capa hacia Dixon-Coles.

Modelo log-lineal clásico de fútbol (Maher 1982): cada equipo tiene una fuerza de
ATAQUE y una de DEFENSA, y existe una VENTAJA DE LOCAL global. Los goles de cada
lado se modelan como Poisson independientes con media:

    log λ_local     = ataque_local + defensa_visitante + ventaja_local
    log λ_visitante = ataque_visitante + defensa_local

Los parámetros se estiman por MÁXIMA VEROSIMILITUD (scipy L-BFGS-B con gradiente
analítico) sobre los goles observados. Con λ se construye la matriz de marcadores
(0..max_goals por lado, goles independientes) y se agregan las probabilidades a
{home, draw, away}.

NO incluye (a propósito, son capas posteriores):
  * la corrección de dependencia de marcadores bajos de Dixon-Coles (HU 3.2);
  * la ponderación temporal por antigüedad (HU 3.3).

Implementa el `PredictionModel` de la Fase 2, así que enchufa en el walk-forward
existente SIN tocarlo.

IDENTIFICABILIDAD
-----------------
Ataque y defensa están confundidos por una constante aditiva: (aₜ+c, dₜ−c) deja
todas las λ idénticas. Es la ÚNICA degeneración del modelo. Se resuelve fijando
la media de ataques en 0 (re-centrado tras optimizar): el punto re-centrado es
único y las predicciones son invariantes a dónde pare el optimizador en la cresta.

EQUIPOS SIN HISTORIA
--------------------
Un equipo no visto en el entrenamiento (recién ascendido — ocurre al inicio de
cada temporada en el walk-forward) recibe fuerza NEUTRA: ataque = media (=0 tras
el re-centrado) y defensa = media de defensas del entrenamiento. Así el modelo lo
trata como un equipo promedio en vez de romper.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln

from valuebet.evaluation.model import Match, Probabilities


@dataclass(frozen=True, slots=True)
class PoissonParameters:
    """Parámetros estimados, inspeccionables para validar que tienen sentido.

    `attack`/`defense` están indexados por `team_id`. Ataque mayor ⇒ marca más;
    defensa MENOR ⇒ encaja menos (la defensa entra como sumando en la λ rival, así
    que valores negativos = mejor defensa).
    """

    attack: dict[uuid.UUID, float]
    defense: dict[uuid.UUID, float]
    home_advantage: float
    mean_defense: float

    def top_attack(self, n: int = 5) -> list[tuple[uuid.UUID, float]]:
        return sorted(self.attack.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def top_defense(self, n: int = 5) -> list[tuple[uuid.UUID, float]]:
        # Mejor defensa = valor más bajo.
        return sorted(self.defense.items(), key=lambda kv: kv[1])[:n]


class PoissonModel:
    """Modelo Poisson de goles que implementa `PredictionModel`."""

    def __init__(self, *, max_goals: int = 10, max_iter: int = 200) -> None:
        if max_goals < 1:
            raise ValueError("max_goals debe ser >= 1")
        self.max_goals = max_goals
        self.max_iter = max_iter

        # Estado tras fit.
        self._teams: list[uuid.UUID] = []
        self._index: dict[uuid.UUID, int] = {}
        self._attack: np.ndarray = np.zeros(0)
        self._defense: np.ndarray = np.zeros(0)
        self._home_adv: float = 0.0
        self._mean_defense: float = 0.0
        self._fitted: bool = False

        # Warm-start: la solución previa acelera el siguiente fit (el walk-forward
        # reajusta tras cada partido y la solución apenas cambia).
        self._prev_attack: dict[uuid.UUID, float] = {}
        self._prev_defense: dict[uuid.UUID, float] = {}
        self._prev_home_adv: float = 0.26  # ventaja de local típica (≈ exp → ×1.3)

    # ------------------------------------------------------------------ #
    # Ajuste (MLE)
    # ------------------------------------------------------------------ #
    def fit(self, matches: Sequence[Match]) -> None:
        finished = [m for m in matches if m.is_finished]
        if not finished:
            self._fitted = False
            return

        teams = sorted({m.home_team_id for m in finished} | {m.away_team_id for m in finished})
        index = {t: i for i, t in enumerate(teams)}
        n = len(teams)

        home_idx = np.fromiter((index[m.home_team_id] for m in finished), dtype=np.intp)
        away_idx = np.fromiter((index[m.away_team_id] for m in finished), dtype=np.intp)
        hg = np.fromiter((m.home_goals for m in finished), dtype=np.float64)
        ag = np.fromiter((m.away_goals for m in finished), dtype=np.float64)

        x0 = self._initial_guess(teams, n)
        result = minimize(
            self._nll_and_grad,
            x0,
            args=(home_idx, away_idx, hg, ag, n),
            jac=True,
            method="L-BFGS-B",
            options={"maxiter": self.max_iter},
        )

        attack = result.x[:n].copy()
        defense = result.x[n : 2 * n].copy()
        home_adv = float(result.x[2 * n])

        # Re-centrado de identificabilidad: media de ataques = 0 (defensa absorbe
        # el desplazamiento, λ invariante).
        shift = float(attack.mean())
        attack -= shift
        defense += shift

        self._teams = teams
        self._index = index
        self._attack = attack
        self._defense = defense
        self._home_adv = home_adv
        self._mean_defense = float(defense.mean())
        self._fitted = True

        # Guarda para warm-start del próximo fit.
        self._prev_attack = {t: float(attack[i]) for t, i in index.items()}
        self._prev_defense = {t: float(defense[i]) for t, i in index.items()}
        self._prev_home_adv = home_adv

    def _initial_guess(self, teams: list[uuid.UUID], n: int) -> np.ndarray:
        x0 = np.zeros(2 * n + 1)
        for t, i in ((t, i) for i, t in enumerate(teams)):
            x0[i] = self._prev_attack.get(t, 0.0)
            x0[n + i] = self._prev_defense.get(t, self._mean_defense)
        x0[2 * n] = self._prev_home_adv
        return x0

    @staticmethod
    def _nll_and_grad(
        x: np.ndarray,
        home_idx: np.ndarray,
        away_idx: np.ndarray,
        hg: np.ndarray,
        ag: np.ndarray,
        n: int,
    ) -> tuple[float, np.ndarray]:
        """Negativo de la log-verosimilitud Poisson y su gradiente analítico.

        Se omite el término constante log(k!) (no depende de los parámetros).
        """
        a = x[:n]
        d = x[n : 2 * n]
        h = x[2 * n]

        log_lh = a[home_idx] + d[away_idx] + h
        log_la = a[away_idx] + d[home_idx]
        lh = np.exp(log_lh)
        la = np.exp(log_la)

        nll = float(np.sum(lh - hg * log_lh) + np.sum(la - ag * log_la))

        # ∂nll/∂(log λ) = λ − goles.
        gh = lh - hg
        ga = la - ag
        grad_a = np.bincount(home_idx, weights=gh, minlength=n) + np.bincount(
            away_idx, weights=ga, minlength=n
        )
        grad_d = np.bincount(away_idx, weights=gh, minlength=n) + np.bincount(
            home_idx, weights=ga, minlength=n
        )
        grad_h = float(np.sum(gh))
        grad = np.concatenate([grad_a, grad_d, [grad_h]])
        return nll, grad

    # ------------------------------------------------------------------ #
    # Predicción
    # ------------------------------------------------------------------ #
    def _strength(self, team_id: uuid.UUID) -> tuple[float, float]:
        """(ataque, defensa) del equipo; fallback NEUTRO si no tiene historia."""
        i = self._index.get(team_id)
        if i is None:
            return 0.0, self._mean_defense  # equipo promedio
        return float(self._attack[i]), float(self._defense[i])

    def _poisson_pmf(self, lam: float) -> np.ndarray:
        ks = np.arange(self.max_goals + 1)
        log_pmf = -lam + ks * np.log(lam) - gammaln(ks + 1)
        return np.exp(log_pmf)

    def predict_proba(self, match: Match) -> Probabilities:
        if not self._fitted:
            return Probabilities.uniform()

        a_home, d_home = self._strength(match.home_team_id)
        a_away, d_away = self._strength(match.away_team_id)

        lambda_home = float(np.exp(a_home + d_away + self._home_adv))
        lambda_away = float(np.exp(a_away + d_home))

        ph = self._poisson_pmf(lambda_home)
        pa = self._poisson_pmf(lambda_away)
        joint = np.outer(ph, pa)  # joint[x, y] = P(local=x, visitante=y)

        home = float(np.tril(joint, -1).sum())  # x > y
        draw = float(np.trace(joint))  # x == y
        away = float(np.triu(joint, 1).sum())  # x < y

        total = home + draw + away  # < 1 por el truncado a max_goals
        if total <= 0.0:  # salvaguarda numérica
            return Probabilities.uniform()
        return Probabilities(home=home / total, draw=draw / total, away=away / total)

    # ------------------------------------------------------------------ #
    # Inspección
    # ------------------------------------------------------------------ #
    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def parameters(self) -> PoissonParameters:
        """Parámetros estimados (ataque/defensa por equipo + ventaja de local)."""
        if not self._fitted:
            raise RuntimeError("el modelo no está ajustado; llama a fit() primero")
        return PoissonParameters(
            attack={t: float(self._attack[i]) for t, i in self._index.items()},
            defense={t: float(self._defense[i]) for t, i in self._index.items()},
            home_advantage=self._home_adv,
            mean_defense=self._mean_defense,
        )
