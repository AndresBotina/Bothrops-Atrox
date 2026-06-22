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

Esta clase está pensada para EXTENDERSE sin duplicar (ver `DixonColesModel`,
HU 3.2): la estructura ataque/defensa/ventaja-local, el ajuste MLE, el warm-start,
el re-centrado, el fallback de equipos sin historia y la agregación de la matriz
de marcadores se reutilizan vía hooks (`_objective`, `_initial_guess`, `_bounds`,
`_store_solution`, `_joint_matrix`).

PONDERACIÓN TEMPORAL (HU 3.3)
-----------------------------
`fit` admite una verosimilitud PONDERADA: cada partido pesa
exp(-ξ·(t_ref − t_partido)), con ξ = ln(2)/half_life y el tiempo en DÍAS. Así el
pasado lejano influye menos. `half_life=None` (∞) ⇒ sin decaimiento (idéntico al
modelo sin ponderar). Como la interfaz `PredictionModel.fit(matches)` no recibe un
`as_of`, se usa t_ref = kickoff MÁXIMO del set de entrenamiento (el walk-forward
sólo pasa partidos anteriores al saque objetivo, así que ese máximo es el "ahora"
del entrenamiento). half_life es un HIPERPARÁMETRO: NO se estima por MLE (las
verosimilitudes ponderadas con distinto ξ no son comparables); se elige por
backtesting (ver `evaluation/tuning.py`).

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

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln

from valuebet.evaluation.model import Match, Probabilities

# Unidad de tiempo de la ponderación temporal (HU 3.3): DÍAS.


def decay_weights(ages_days: np.ndarray, half_life: float | None) -> np.ndarray:
    """Pesos de decaimiento exponencial por antigüedad (en días).

    w = exp(-ξ · edad) con ξ = ln(2)/half_life. Así un partido con antigüedad de
    una half-life pesa 0.5, dos half-lives 0.25, etc. `half_life=None` (∞) ⇒ ξ=0
    ⇒ todos los pesos valen 1.0 (sin decaimiento; equivale a la HU 3.2).
    """
    if half_life is None:
        return np.ones_like(ages_days)
    if half_life <= 0:
        raise ValueError("half_life debe ser > 0 (o None para infinito)")
    xi = math.log(2.0) / half_life
    return np.exp(-xi * ages_days)


@dataclass(frozen=True, slots=True)
class PoissonParameters:
    """Parámetros estimados, inspeccionables para validar que tienen sentido.

    `attack`/`defense` están indexados por `team_id`. Ataque mayor ⇒ marca más;
    defensa MENOR ⇒ encaja menos (la defensa entra como sumando en la λ rival, así
    que valores negativos = mejor defensa). `rho` es la dependencia de marcadores
    bajos de Dixon-Coles (None en el Poisson puro, donde no existe).
    """

    attack: dict[uuid.UUID, float]
    defense: dict[uuid.UUID, float]
    home_advantage: float
    mean_defense: float
    rho: float | None = None

    def top_attack(self, n: int = 5) -> list[tuple[uuid.UUID, float]]:
        return sorted(self.attack.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def top_defense(self, n: int = 5) -> list[tuple[uuid.UUID, float]]:
        # Mejor defensa = valor más bajo.
        return sorted(self.defense.items(), key=lambda kv: kv[1])[:n]


class PoissonModel:
    """Modelo Poisson de goles que implementa `PredictionModel`."""

    def __init__(
        self, *, max_goals: int = 10, max_iter: int = 200, half_life: float | None = None
    ) -> None:
        if max_goals < 1:
            raise ValueError("max_goals debe ser >= 1")
        if half_life is not None and half_life <= 0:
            raise ValueError("half_life debe ser > 0 (o None para infinito = sin decaimiento)")
        self.max_goals = max_goals
        self.max_iter = max_iter
        self.half_life = half_life  # días; None = sin decaimiento temporal

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

        # Pesos de decaimiento temporal: t_ref = kickoff máximo del entrenamiento.
        t_ref = max(m.kickoff_utc for m in finished)
        ages = np.fromiter(
            ((t_ref - m.kickoff_utc).total_seconds() / 86400.0 for m in finished),
            dtype=np.float64,
            count=len(finished),
        )
        weights = decay_weights(ages, self.half_life)

        x0 = self._initial_guess(teams, n)
        result = minimize(
            self._objective,
            x0,
            args=(home_idx, away_idx, hg, ag, weights, n),
            jac=True,
            method="L-BFGS-B",
            bounds=self._bounds(n),
            options={"maxiter": self.max_iter},
        )
        self._store_solution(result.x, teams, index, n)

    def _initial_guess(self, teams: list[uuid.UUID], n: int) -> np.ndarray:
        x0 = np.zeros(2 * n + 1)
        for i, t in enumerate(teams):
            x0[i] = self._prev_attack.get(t, 0.0)
            x0[n + i] = self._prev_defense.get(t, self._mean_defense)
        x0[2 * n] = self._prev_home_adv
        return x0

    def _bounds(self, n: int) -> list[tuple[float | None, float | None]] | None:
        """Cotas para el optimizador. Poisson no las necesita (todo libre)."""
        return None

    def _store_solution(
        self, x: np.ndarray, teams: list[uuid.UUID], index: dict[uuid.UUID, int], n: int
    ) -> None:
        attack = x[:n].copy()
        defense = x[n : 2 * n].copy()
        home_adv = float(x[2 * n])

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

    def _objective(
        self,
        x: np.ndarray,
        home_idx: np.ndarray,
        away_idx: np.ndarray,
        hg: np.ndarray,
        ag: np.ndarray,
        weights: np.ndarray,
        n: int,
    ) -> tuple[float, np.ndarray]:
        """Negativo de la log-verosimilitud Poisson PONDERADA y su gradiente.

        Cada partido pesa `weights[m]` (decaimiento temporal). Se omite el término
        constante log(k!) (no depende de los parámetros). Con weights ≡ 1 coincide
        exactamente con la verosimilitud sin ponderar.
        """
        a = x[:n]
        d = x[n : 2 * n]
        h = x[2 * n]

        log_lh = a[home_idx] + d[away_idx] + h
        log_la = a[away_idx] + d[home_idx]
        lh = np.exp(log_lh)
        la = np.exp(log_la)

        nll = float(np.sum(weights * (lh - hg * log_lh)) + np.sum(weights * (la - ag * log_la)))

        # ∂nll/∂(log λ) = peso · (λ − goles).
        gh = weights * (lh - hg)
        ga = weights * (la - ag)
        grad = self._chain_grad(gh, ga, home_idx, away_idx, n)
        return nll, grad

    @staticmethod
    def _chain_grad(
        gh: np.ndarray, ga: np.ndarray, home_idx: np.ndarray, away_idx: np.ndarray, n: int
    ) -> np.ndarray:
        """Encadena ∂nll/∂(log λ_local)=gh y ∂nll/∂(log λ_visit)=ga a (a, d, h)."""
        grad_a = np.bincount(home_idx, weights=gh, minlength=n) + np.bincount(
            away_idx, weights=ga, minlength=n
        )
        grad_d = np.bincount(away_idx, weights=gh, minlength=n) + np.bincount(
            home_idx, weights=ga, minlength=n
        )
        grad_h = float(np.sum(gh))
        return np.concatenate([grad_a, grad_d, [grad_h]])

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

    def _joint_matrix(self, lambda_home: float, lambda_away: float) -> np.ndarray:
        """Matriz P(local=x, visitante=y). Poisson: producto externo (independencia).

        Hook de extensión: Dixon-Coles aplica aquí su corrección a las 4 celdas bajas.
        """
        ph = self._poisson_pmf(lambda_home)
        pa = self._poisson_pmf(lambda_away)
        return np.outer(ph, pa)

    def predict_proba(self, match: Match) -> Probabilities:
        if not self._fitted:
            return Probabilities.uniform()

        a_home, d_home = self._strength(match.home_team_id)
        a_away, d_away = self._strength(match.away_team_id)

        lambda_home = float(np.exp(a_home + d_away + self._home_adv))
        lambda_away = float(np.exp(a_away + d_home))

        joint = self._joint_matrix(lambda_home, lambda_away)
        joint = np.clip(joint, 0.0, None)  # la corrección DC podría dar negativos ínfimos

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
