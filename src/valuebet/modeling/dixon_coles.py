"""Corrección DIXON-COLES sobre el modelo Poisson (HU 3.2).

El Poisson independiente subestima los marcadores bajos (0-0, 1-0, 0-1, 1-1) y,
con ellos, los empates. Dixon & Coles (1997) introducen una función de dependencia
τ(x, y; λ, μ, ρ) que ajusta SÓLO esas cuatro celdas, con un parámetro ρ estimado
junto al resto por máxima verosimilitud:

    τ(0,0) = 1 − λ·μ·ρ
    τ(0,1) = 1 + λ·ρ
    τ(1,0) = 1 + μ·ρ
    τ(1,1) = 1 − ρ
    resto  = 1

donde λ = goles esperados local, μ = goles esperados visitante. La densidad pasa a
ser  P(x,y) = τ(x,y) · Poisson(x; λ) · Poisson(y; μ).  En fútbol ρ suele ser
NEGATIVO y pequeño (~ −0.1): con ρ<0, τ sube en (0,0) y (1,1) → más empates.

`DixonColesModel` HEREDA de `PoissonModel` y reutiliza toda su maquinaria
(ataque/defensa/ventaja-local, MLE, warm-start, re-centrado, fallback de equipos
sin historia, agregación de la matriz). Sólo añade ρ:
  * `_objective`: la log-verosimilitud incluye el factor τ (con gradiente analítico).
  * `_joint_matrix`: aplica τ a las 4 celdas bajas antes de normalizar.
  * el vector de parámetros gana una componente (ρ), con su cota e inicialización.

Con ρ = 0, τ ≡ 1 y el modelo es IDÉNTICO al Poisson puro (consistencia).

NO incluye la ponderación temporal (HU 3.3).
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import numpy as np

from valuebet.modeling.poisson import PoissonModel, PoissonParameters

# Cota de ρ para el optimizador. Mantiene τ positivo para λ/μ realistas (un ρ muy
# negativo haría τ(0,1)=1+λρ < 0). El rango cubre de sobra el valor típico (~ −0.1)
# y el dominio de fútbol; valores fuera de esto serían un síntoma de problema.
_RHO_LO, _RHO_HI = -0.4, 0.4

# Suelo numérico de τ en la verosimilitud: si una combinación inválida hace τ ≤ 0,
# se penaliza fuerte (log de un número ínfimo) y su gradiente se anula, de modo que
# la búsqueda de línea rechaza ese paso y el optimizador se queda en la región válida.
_TAU_FLOOR = 1e-10


class DixonColesModel(PoissonModel):
    """Modelo Poisson + corrección Dixon-Coles de marcadores bajos."""

    def __init__(self, *, max_goals: int = 10, max_iter: int = 200) -> None:
        super().__init__(max_goals=max_goals, max_iter=max_iter)
        self._rho: float = 0.0
        self._prev_rho: float = -0.10  # arranque típico en fútbol

    # ------------------------------------------------------------------ #
    # Ajuste: vector de parámetros con ρ añadido al final
    # ------------------------------------------------------------------ #
    def _initial_guess(self, teams: list[uuid.UUID], n: int) -> np.ndarray:
        base = super()._initial_guess(teams, n)  # longitud 2n+1
        return np.concatenate([base, [self._prev_rho]])

    def _bounds(self, n: int) -> list[tuple[float | None, float | None]]:
        # Ataque/defensa/ventaja-local libres; ρ acotado.
        return [(None, None)] * (2 * n + 1) + [(_RHO_LO, _RHO_HI)]

    def _store_solution(
        self, x: np.ndarray, teams: list[uuid.UUID], index: dict[uuid.UUID, int], n: int
    ) -> None:
        super()._store_solution(x, teams, index, n)  # ignora la componente extra
        self._rho = float(x[2 * n + 1])
        self._prev_rho = self._rho

    def _objective(
        self,
        x: np.ndarray,
        home_idx: np.ndarray,
        away_idx: np.ndarray,
        hg: np.ndarray,
        ag: np.ndarray,
        n: int,
    ) -> tuple[float, np.ndarray]:
        """NLL Dixon-Coles = NLL Poisson − Σ log τ, con gradiente analítico.

        τ depende de λ=exp(log λ_local), μ=exp(log λ_visit) y ρ; sólo contribuye en
        las 4 celdas bajas según el marcador OBSERVADO de cada partido.
        """
        a = x[:n]
        d = x[n : 2 * n]
        h = x[2 * n]
        rho = x[2 * n + 1]

        log_lh = a[home_idx] + d[away_idx] + h
        log_la = a[away_idx] + d[home_idx]
        lh = np.exp(log_lh)
        la = np.exp(log_la)

        # --- parte Poisson (idéntica a la base) ---
        nll = float(np.sum(lh - hg * log_lh) + np.sum(la - ag * log_la))
        gh = lh - hg  # ∂nll_pois/∂(log λ_local)
        ga = la - ag  # ∂nll_pois/∂(log λ_visit)

        # --- corrección τ (sólo celdas bajas según el marcador observado) ---
        m00 = (hg == 0) & (ag == 0)
        m01 = (hg == 0) & (ag == 1)
        m10 = (hg == 1) & (ag == 0)
        m11 = (hg == 1) & (ag == 1)

        tau = np.ones_like(lh)
        tau[m00] = 1.0 - lh[m00] * la[m00] * rho
        tau[m01] = 1.0 + lh[m01] * rho
        tau[m10] = 1.0 + la[m10] * rho
        tau[m11] = 1.0 - rho

        bad = tau <= _TAU_FLOOR
        tau_safe = np.where(bad, _TAU_FLOOR, tau)
        nll -= float(np.sum(np.log(tau_safe)))

        # ∂(log τ)/∂(log λ_local), ∂/∂(log λ_visit), ∂/∂ρ por celda.
        dlt_dL = np.zeros_like(lh)
        dlt_dM = np.zeros_like(lh)
        dlt_drho = np.zeros_like(lh)

        prod = lh * la  # λ·μ
        dlt_dL[m00] = -prod[m00] * rho / tau_safe[m00]
        dlt_dM[m00] = -prod[m00] * rho / tau_safe[m00]
        dlt_drho[m00] = -prod[m00] / tau_safe[m00]

        dlt_dL[m01] = lh[m01] * rho / tau_safe[m01]
        dlt_drho[m01] = lh[m01] / tau_safe[m01]

        dlt_dM[m10] = la[m10] * rho / tau_safe[m10]
        dlt_drho[m10] = la[m10] / tau_safe[m10]

        dlt_drho[m11] = -1.0 / tau_safe[m11]

        # Donde τ se tuvo que pisar, el gradiente local se anula (subgradiente 0).
        if bad.any():
            keep = ~bad
            dlt_dL *= keep
            dlt_dM *= keep
            dlt_drho *= keep

        # nll = nll_pois − Σ log τ  ⇒  ∂nll/∂(log λ) = g_pois − ∂log τ/∂(log λ).
        gh_eff = gh - dlt_dL
        ga_eff = ga - dlt_dM
        grad = self._chain_grad(gh_eff, ga_eff, home_idx, away_idx, n)
        grad_rho = -float(np.sum(dlt_drho))
        return nll, np.concatenate([grad, [grad_rho]])

    # ------------------------------------------------------------------ #
    # Predicción: aplica τ a la matriz de marcadores
    # ------------------------------------------------------------------ #
    def _joint_matrix(self, lambda_home: float, lambda_away: float) -> np.ndarray:
        joint = super()._joint_matrix(lambda_home, lambda_away)  # producto Poisson
        rho = self._rho
        joint[0, 0] *= 1.0 - lambda_home * lambda_away * rho
        joint[0, 1] *= 1.0 + lambda_home * rho
        joint[1, 0] *= 1.0 + lambda_away * rho
        joint[1, 1] *= 1.0 - rho
        return joint

    # ------------------------------------------------------------------ #
    # Inspección: parámetros + ρ
    # ------------------------------------------------------------------ #
    @property
    def rho(self) -> float:
        if not self._fitted:
            raise RuntimeError("el modelo no está ajustado; llama a fit() primero")
        return self._rho

    @property
    def parameters(self) -> PoissonParameters:
        return replace(super().parameters, rho=self._rho)
